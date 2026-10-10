"""Child side of ``run_code_in_subprocess``: run the code and forward ``tools.*`` calls to Bub.

Bub runs this file with ``python -c`` inside the environment, so it must only use
the standard library. Both directions exchange JSON lines:

- Bub writes ``{"type": "run", "code", "filename", "tools"}`` to stdin first,
  then ``{"type": "result", "id", "value"}`` or ``{"type": "error", "id", "message"}``
  for each tool call.
- The runner writes ``{"type": "call", "id", "name", "arguments"}`` to stdout for
  each tool call, ``{"type": "output", "data"}`` for what the code writes to
  ``sys.stdout``, and ends with ``{"type": "done"}`` or
  ``{"type": "failed", "error", "traceback"}``.

The code also gets a ``republic`` module (importable and preset as a global). Its models
call Bub through tool calls named ``republic.<operation>``, so credentials stay in Bub.
Republic values travel as JSON objects with a ``"$republic"`` key, such as
``{"$republic": "image", "media_type", "data", "url"}`` with base64 ``data``.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import builtins
import inspect
import io
import itertools
import json
import linecache
import sys
import threading
import time
import traceback
import types
from typing import Any

_OUTPUT_FLUSH_SIZE = 1 << 13
_OUTPUT_FLUSH_INTERVAL = 0.2


class BubError(Exception):
    """A tool call failed or was denied on the Bub side."""


_REPUBLIC_KEY = "$republic"


class _Media:
    kind = "media"

    def __init__(self, media_type: str, data: bytes | None = None, url: str | None = None) -> None:
        if data is None and url is None:
            raise ValueError(f"{type(self).__name__} needs data or a url")
        self.media_type = media_type
        self.data = data
        self.url = url

    @property
    def base64_data(self) -> str:
        if self.data is None:
            raise ValueError(f"This {self.kind} has no inline data")
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        if self.url is not None:
            return self.url
        return f"data:{self.media_type};base64,{self.base64_data}"

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and (self.media_type, self.data, self.url) == (
            other.media_type,
            other.data,
            other.url,
        )

    def __hash__(self) -> int:
        return hash((type(self), self.media_type, self.data, self.url))

    def __repr__(self) -> str:
        source = f"url={self.url!r}" if self.url is not None else f"data=<{len(self.data or b'')} bytes>"
        return f"{type(self).__name__}(media_type={self.media_type!r}, {source})"

    def _to_json(self) -> dict[str, Any]:
        data = None if self.data is None else self.base64_data
        return {_REPUBLIC_KEY: self.kind, "media_type": self.media_type, "data": data, "url": self.url}


class Image(_Media):
    kind = "image"


class Audio(_Media):
    kind = "audio"


class Video(_Media):
    kind = "video"


_MEDIA_CLASSES: dict[str, type[_Media]] = {"image": Image, "audio": Audio, "video": Video}


class Message:
    """A chat message whose content mixes text and media."""

    def __init__(self, role: str, content: list[Any]) -> None:
        self.role = role
        self.content = content

    @property
    def text(self) -> str:
        return "".join(item for item in self.content if isinstance(item, str))

    def __repr__(self) -> str:
        return f"Message(role={self.role!r}, content={self.content!r})"

    def _to_json(self) -> dict[str, Any]:
        return {_REPUBLIC_KEY: "message", "role": self.role, "content": self.content}


def system(text: str) -> Message:
    return Message("system", [text])


def user(*content: Any) -> Message:
    return Message("user", list(content))


def assistant(*content: Any) -> Message:
    return Message("assistant", list(content))


class _Question:
    kind = "question"

    def __init__(self, instructions: Any, criteria: Any = None) -> None:
        self.instructions = instructions
        self.criteria = criteria

    def __repr__(self) -> str:
        return f"{type(self).__name__}(instructions={self.instructions!r}, criteria={self.criteria!r})"

    def _to_json(self) -> dict[str, Any]:
        return {_REPUBLIC_KEY: self.kind, "instructions": self.instructions, "criteria": self.criteria}


class Noul(_Question):
    """A yes/no question answered with the probability of yes."""

    kind = "noul"


class Choice(_Question):
    """Pick one option; ``criteria`` maps options to descriptions or lists option names."""

    kind = "choice"

    def __init__(self, instructions: Any, criteria: Any) -> None:
        super().__init__(instructions, criteria)


class Score(_Question):
    """Rate along ordered levels, from lowest to highest."""

    kind = "score"

    def __init__(self, instructions: Any, criteria: Any) -> None:
        super().__init__(instructions, criteria)


class _Record(types.SimpleNamespace):
    """A result whose fields are attributes."""


class DecisionResponse(_Record):
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self.__dict__["answers"][name]
        except KeyError:
            raise AttributeError(name) from None


class EmbeddingResponse(_Record):
    @property
    def vector(self) -> list[float]:
        vectors: list[list[float]] = self.vectors
        return vectors[0]


def _record(value: Any, record_class: type[_Record] = _Record) -> Any:
    if isinstance(value, dict) and _REPUBLIC_KEY not in value:
        return record_class(**{key: _record(item) for key, item in value.items()})
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, _Media | Message | _Question):
        return value._to_json()
    if isinstance(value, set | frozenset):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_object(value: dict[str, Any]) -> Any:
    kind = value.get(_REPUBLIC_KEY)
    if kind in _MEDIA_CLASSES:
        data = value.get("data")
        return _MEDIA_CLASSES[kind](
            value["media_type"], data=None if data is None else base64.b64decode(data), url=value.get("url")
        )
    if kind == "message":
        return Message(value["role"], list(value.get("content") or []))
    return value


class _Model:
    def __init__(self, call: Any, spec: str | None) -> None:
        self._call = call
        self.spec = spec

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.spec!r})"


class TextDelta(_Record):
    """``chunk`` of answer text."""


class ReasoningDelta(_Record):
    """``chunk`` of reasoning text, or its summary."""


class RefusalDelta(_Record):
    """``chunk`` of text explaining why the model declines to answer."""


class UsageDelta(_Record):
    """``usage`` added since the previous usage event."""


class ImageReady(_Record):
    """An ``image`` generated by the model."""


class CitationAdded(_Record):
    """A ``citation`` of a source."""


class Completed(_Record):
    """The last event, carrying the full ``response``."""


class OtherEvent(_Record):
    """An ``event`` (its type name) that is not forwarded to code."""


_EVENT_CLASSES: dict[str, type[_Record]] = {
    "text": TextDelta,
    "reasoning": ReasoningDelta,
    "refusal": RefusalDelta,
    "usage": UsageDelta,
    "image": ImageReady,
    "citation": CitationAdded,
    "completed": Completed,
}


def _event(data: dict[str, Any]) -> _Record:
    kind = data.pop("type")
    event_class = _EVENT_CLASSES.get(kind, OtherEvent)
    return event_class(**{key: _record(value) for key, value in data.items()})


def _chat_arguments(spec: str | None, prompt: Any, options: dict[str, Any]) -> dict[str, Any]:
    if isinstance(prompt, str | Message):
        prompt = [prompt]
    messages = [user(item) if isinstance(item, str) else item for item in prompt]
    return {"spec": spec, "messages": messages, "options": options}


class Stream:
    """An in-progress response: ``async with`` it, then ``async for`` its events.

    After the iteration ends, the full response is ``stream.response``, with shortcuts
    ``text``, ``reasoning`` and ``token_usage``.
    """

    def __init__(self, call: Any, arguments: dict[str, Any]) -> None:
        self._call = call
        self._arguments = arguments
        self._id: str | None = None
        self._iterated = False
        self._response: Any = None

    async def __aenter__(self) -> Stream:
        self._id = (await self._call("republic.stream_open", self._arguments))["stream"]
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._id is not None:
            stream_id, self._id = self._id, None
            await self._call("republic.stream_close", {"stream": stream_id})

    async def __aiter__(self) -> Any:
        if self._id is None:
            raise RuntimeError("Enter the stream with 'async with' before iterating it")
        if self._iterated:
            raise RuntimeError("A stream can only be iterated once; read stream.response instead")
        self._iterated = True
        while True:
            batch = await self._call("republic.stream_next", {"stream": self._id})
            for data in batch["events"]:
                event = _event(data)
                if isinstance(event, Completed):
                    self._response = event.response
                yield event
            if batch["done"]:
                self._id = None
                return

    @property
    def response(self) -> Any:
        if self._response is None:
            raise RuntimeError("The stream has not been fully consumed")
        return self._response

    @property
    def text(self) -> str:
        text: str = self.response.text
        return text

    @property
    def reasoning(self) -> str:
        reasoning: str = self.response.reasoning
        return reasoning

    @property
    def token_usage(self) -> Any:
        return self.response.token_usage


class ChatModel(_Model):
    """A chat model served by Bub."""

    async def chat(self, prompt: Any, **options: Any) -> Any:
        """Send the conversation and wait for the complete response."""
        return _record(await self._call("republic.chat", _chat_arguments(self.spec, prompt, options)))

    def stream(self, prompt: Any, **options: Any) -> Stream:
        """Stream the response. Use as ``async with model.stream(...) as stream``."""
        return Stream(self._call, _chat_arguments(self.spec, prompt, options))


class EmbeddingModel(_Model):
    """An embedding model served by Bub."""

    async def embed(self, text: str, *, dimensions: int | None = None) -> Any:
        """Embed one text; the vector is ``response.vector``."""
        return await self.embed_many([text], dimensions=dimensions)

    async def embed_many(self, texts: list[str], *, dimensions: int | None = None) -> Any:
        """Embed several texts in one request; ``response.vectors`` follows input order."""
        arguments = {"spec": self.spec, "texts": list(texts), "dimensions": dimensions}
        return _record(await self._call("republic.embed", arguments), EmbeddingResponse)


class DecisionModel(_Model):
    """A decision model served by Bub."""

    async def decide(self, state: Any, *, questions: dict[str, _Question]) -> Any:
        """Answer every question about ``state``. Answers come back under the same ids."""
        result = await self._call("republic.decide", {"spec": self.spec, "state": state, "questions": questions})
        response = _record(result, DecisionResponse)
        response.answers = {key: _record(answer) for key, answer in result["answers"].items()}
        return response


def _republic_module(call: Any) -> types.ModuleType:
    module = types.ModuleType("republic", "Republic models and media, served by Bub.")
    events = types.ModuleType("republic.events")
    events.__dict__.update(
        TextDelta=TextDelta,
        ReasoningDelta=ReasoningDelta,
        RefusalDelta=RefusalDelta,
        UsageDelta=UsageDelta,
        ImageReady=ImageReady,
        CitationAdded=CitationAdded,
        Completed=Completed,
        OtherEvent=OtherEvent,
    )
    decisions = types.ModuleType("republic.decisions")
    decisions.__dict__.update(Noul=Noul, Choice=Choice, Score=Score, DecisionResponse=DecisionResponse)

    def get_model(spec: str | None = None) -> ChatModel:
        """A chat model for a ``"provider:model"`` spec; defaults to the model running this session."""
        return ChatModel(call, spec)

    def get_embedding_model(spec: str) -> EmbeddingModel:
        """An embedding model for a ``"provider:model"`` spec."""
        return EmbeddingModel(call, spec)

    def get_decision_model(spec: str) -> DecisionModel:
        """A decision model for a ``"provider:model"`` spec."""
        return DecisionModel(call, spec)

    module.__dict__.update(
        Image=Image,
        Audio=Audio,
        Video=Video,
        Message=Message,
        ChatModel=ChatModel,
        EmbeddingModel=EmbeddingModel,
        DecisionModel=DecisionModel,
        EmbeddingResponse=EmbeddingResponse,
        system=system,
        user=user,
        assistant=assistant,
        decisions=decisions,
        events=events,
        Stream=Stream,
        get_model=get_model,
        get_embedding_model=get_embedding_model,
        get_decision_model=get_decision_model,
    )
    return module


class _Channel:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._out = sys.stdout
        self._send_lock = threading.Lock()
        self._ids = itertools.count()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._first: asyncio.Future[dict[str, Any]] = loop.create_future()

    def send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, default=_json_default) + "\n"
        with self._send_lock:
            self._out.write(line)
            self._out.flush()

    def start(self) -> asyncio.Future[dict[str, Any]]:
        # A daemon thread keeps reading stdin without blocking interpreter exit.
        threading.Thread(target=self._read, daemon=True).start()
        return self._first

    def _read(self) -> None:
        for line in sys.stdin:
            self._loop.call_soon_threadsafe(self._dispatch, json.loads(line, object_hook=_json_object))
        self._loop.call_soon_threadsafe(self._close)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not self._first.done():
            self._first.set_result(message)
            return
        future = self._pending.pop(message.get("id"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if message.get("type") == "result":
            future.set_result(message.get("value"))
        else:
            future.set_exception(BubError(message.get("message", "tool call failed")))

    def _close(self) -> None:
        if not self._first.done():
            self._first.set_exception(EOFError("Bub closed the connection"))
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BubError("Bub closed the connection"))
        self._pending.clear()

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        call_id = next(self._ids)
        future = self._loop.create_future()
        self._pending[call_id] = future
        try:
            self.send({"type": "call", "id": call_id, "name": name, "arguments": arguments})
            return await future
        finally:
            self._pending.pop(call_id, None)


class _Output(io.TextIOBase):
    """Forward what the code prints to Bub in chunks, so output survives a timeout.

    A daemon thread also flushes periodically, because blocking code stalls the event loop.
    """

    def __init__(self, channel: _Channel) -> None:
        self._channel = channel
        self._parts: list[str] = []
        self._size = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._flush_periodically, daemon=True).start()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
            self._size += len(text)
            full = self._size >= _OUTPUT_FLUSH_SIZE
        if full:
            self.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._parts:
                data = "".join(self._parts)
                self._parts.clear()
                self._size = 0
                self._channel.send({"type": "output", "data": data})

    def _flush_periodically(self) -> None:
        while True:
            time.sleep(_OUTPUT_FLUSH_INTERVAL)
            self.flush()


def _tool_function(channel: _Channel, output: _Output, name: str) -> Any:
    async def call(*args: Any, **kwargs: Any) -> Any:
        if args:
            raise TypeError(f"tools.{name}() accepts keyword arguments only")
        output.flush()
        return await channel.call(name, kwargs)

    call.__name__ = name
    return call


async def _main() -> None:
    channel = _Channel(asyncio.get_running_loop())
    run = await channel.start()
    code, filename = run["code"], run["filename"]
    output = _Output(channel)
    sys.stdout = output

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        output.flush()
        return await channel.call(name, arguments)

    # Shadow any installed Republic: models must go through Bub, which holds the credentials.
    republic = sys.modules["republic"] = _republic_module(call)
    sys.modules["republic.decisions"] = republic.decisions
    sys.modules["republic.events"] = republic.events
    namespace = {
        "__name__": "__run_code__",
        "__builtins__": builtins,
        "tools": types.SimpleNamespace(**{name: _tool_function(channel, output, name) for name in run["tools"]}),
        "republic": republic,
    }
    linecache.cache[filename] = (len(code), None, code.splitlines(keepends=True), filename)
    try:
        compiled = compile(code, filename, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        # With top-level await enabled, eval returns a coroutine when the code awaits anything.
        result = eval(compiled, namespace)  # noqa: S307
        if inspect.iscoroutine(result):
            await result
    except (Exception, SystemExit) as exc:
        error = traceback.TracebackException.from_exception(exc)
        # Keep only frames from the model's code; runner frames are noise to the model.
        error.stack = traceback.StackSummary.from_list([frame for frame in error.stack if frame.filename == filename])
        output.flush()
        channel.send({"type": "failed", "error": f"{type(exc).__name__}: {exc}", "traceback": "".join(error.format())})
    else:
        output.flush()
        channel.send({"type": "done"})


if __name__ == "__main__":
    asyncio.run(_main())
