"""Serve the ``republic`` module of ``run_code``: Republic values over JSON and model calls made in Bub.

Model-written code never sees provider credentials: its models are proxies whose calls arrive
here as tool calls named ``republic.<operation>`` (see ``code_runner_child``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import asdict
from typing import Any

import republic
from pydantic_core import to_jsonable_python
from republic.decisions import Choice, Noul, Question, Score
from republic.events import (
    CitationAdded,
    Completed,
    Event,
    ImageReady,
    ReasoningDelta,
    RefusalDelta,
    TextDelta,
    UsageDelta,
)

from bub.errors import BubError, ErrorKind
from bub.tools import ToolContext

REPUBLIC_CALL_PREFIX = "republic."
REPUBLIC_KEY = "$republic"

_MEDIA_CLASSES: dict[str, type[republic.Image | republic.Audio | republic.Video]] = {
    "image": republic.Image,
    "audio": republic.Audio,
    "video": republic.Video,
}
_QUESTION_CLASSES: dict[str, type[Noul | Choice | Score]] = {"noul": Noul, "choice": Choice, "score": Score}
_MESSAGE_BUILDERS: dict[str, Callable[..., republic.Message]] = {
    "system": republic.system,
    "user": republic.user,
    "assistant": republic.assistant,
}


def decode_value(value: Any) -> Any:
    """Turn ``{"$republic": ...}`` objects sent by the code into Republic values."""
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(REPUBLIC_KEY)
    if kind is None:
        return {key: decode_value(item) for key, item in value.items()}
    try:
        if kind in _MEDIA_CLASSES:
            data = value.get("data")
            return _MEDIA_CLASSES[kind](
                value["media_type"],
                data=None if data is None else base64.b64decode(data, validate=True),
                url=value.get("url"),
            )
        if kind in _QUESTION_CLASSES:
            arguments = {"instructions": value["instructions"]}
            if value.get("criteria") is not None or kind != "noul":
                arguments["criteria"] = value.get("criteria")
            return _QUESTION_CLASSES[kind](**arguments)
        if kind == "message":
            return _MESSAGE_BUILDERS[value["role"]](*decode_value(value.get("content") or []))
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise BubError(ErrorKind.INVALID_INPUT, f"Invalid republic {kind} value: {exc}") from exc
    raise BubError(ErrorKind.INVALID_INPUT, f"Unknown republic value: {kind!r}")


def encode_value(value: Any) -> Any:
    """Turn Republic media in a result into ``{"$republic": ...}`` objects the code decodes."""
    if isinstance(value, republic.Image | republic.Audio | republic.Video):
        data = None if value.data is None else value.base64_data
        return {REPUBLIC_KEY: value.kind, "media_type": value.media_type, "data": data, "url": value.url}
    if isinstance(value, list | tuple):
        return [encode_value(item) for item in value]
    if isinstance(value, Mapping):
        return {key: encode_value(item) for key, item in value.items()}
    return value


def _encode_message(message: republic.Message) -> dict[str, Any]:
    content = [
        part.text if isinstance(part, republic.Text) else encode_value(part)
        for part in message.parts
        if isinstance(part, republic.Text | republic.Image | republic.Audio | republic.Video)
    ]
    return {REPUBLIC_KEY: "message", "role": message.role, "content": content}


def _model_spec(context: ToolContext, spec: str | None) -> tuple[str, dict[str, Any]]:
    """Resolve a spec, or the session's model, to a Republic spec and client options from Bub's settings."""
    agent = context.state.get("_runtime_agent")
    settings = getattr(agent, "settings", None)
    if settings is None:
        raise BubError(ErrorKind.INVALID_INPUT, "Republic models need a running Bub agent.")
    spec = spec or context.state.get("model") or settings.model
    try:
        candidate = settings.model_candidates(spec)[0]
    except ValueError as exc:
        raise BubError(ErrorKind.INVALID_INPUT, str(exc)) from exc
    client_kwargs = settings.model_client_kwargs(candidate.provider_name or candidate.provider)
    return f"{candidate.provider}:{candidate.model_id}", client_kwargs


def _chat_messages(messages: list[Any], options: dict[str, Any]) -> list[republic.Message]:
    if "tools" in options or "tool_choice" in options:
        raise BubError(ErrorKind.INVALID_INPUT, "republic models in run_code do not support tools.")
    if not all(isinstance(message, republic.Message) for message in messages):
        raise BubError(ErrorKind.INVALID_INPUT, "chat() takes text or republic messages.")
    return messages


def _encode_response(response: republic.Response[Any]) -> dict[str, Any]:
    return {
        "text": response.text,
        "reasoning": response.reasoning,
        "refusal": response.refusal,
        "finish_reason": response.finish_reason,
        "model": response.model,
        "token_usage": asdict(response.token_usage),
        "message": _encode_message(response.message),
    }


def _encode_event(event: Event) -> dict[str, Any]:
    match event:
        case TextDelta(chunk):
            return {"type": "text", "chunk": chunk}
        case ReasoningDelta(chunk):
            return {"type": "reasoning", "chunk": chunk}
        case RefusalDelta(chunk):
            return {"type": "refusal", "chunk": chunk}
        case UsageDelta(usage):
            return {"type": "usage", "usage": asdict(usage)}
        case ImageReady(image):
            return {"type": "image", "image": encode_value(image)}
        case CitationAdded(citation):
            return {"type": "citation", "citation": to_jsonable_python(citation)}
        case Completed(response):
            return {"type": "completed", "response": _encode_response(response)}
        case _:
            return {"type": "other", "event": type(event).__name__}


_END = object()


class _StreamPump:
    """Read a Republic stream in the background so each ``stream_next`` returns every event that has arrived."""

    def __init__(self, stack: AsyncExitStack, stream: republic.Stream[Any]) -> None:
        self._stack = stack
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task = asyncio.create_task(self._pump(stream))

    async def _pump(self, stream: republic.Stream[Any]) -> None:
        try:
            async for event in stream:
                self._queue.put_nowait(_encode_event(event))
        except Exception as exc:
            self._queue.put_nowait(exc)
        finally:
            self._queue.put_nowait(_END)

    async def next(self) -> dict[str, Any]:
        items = [await self._queue.get()]
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        events = [item for item in items if isinstance(item, dict)]
        if error := next((item for item in items if isinstance(item, Exception)), None):
            if events:
                # Deliver the events received before the failure first.
                self._queue.put_nowait(error)
                self._queue.put_nowait(_END)
                return {"events": events, "done": False}
            raise error
        return {"events": events, "done": _END in items}

    async def aclose(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self._stack.aclose()


class RepublicSession:
    """Serve the ``republic.<operation>`` calls of one ``run_code``; ``aclose`` closes streams left open."""

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._streams: dict[str, _StreamPump] = {}
        self._operations: dict[str, Callable[..., Awaitable[Any]]] = {
            "chat": self._chat,
            "stream_open": self._stream_open,
            "stream_next": self._stream_next,
            "stream_close": self._stream_close,
            "embed": self._embed,
            "decide": self._decide,
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run the ``republic.<operation>`` call ``name`` made by the code's ``republic`` module."""
        operation = self._operations.get(name.removeprefix(REPUBLIC_CALL_PREFIX))
        if operation is None:
            raise BubError(ErrorKind.INVALID_INPUT, f"unknown republic operation: {name}")
        try:
            return await operation(**arguments)
        except BubError:
            raise
        except TypeError as exc:
            raise BubError(ErrorKind.INVALID_INPUT, f"{name} failed: {exc}") from exc
        except Exception as exc:
            raise BubError(ErrorKind.TOOL, f"{name} failed: {type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        streams, self._streams = list(self._streams.values()), {}
        await asyncio.gather(*(stream.aclose() for stream in streams), return_exceptions=True)

    async def _chat(self, spec: str | None, messages: list[Any], options: dict[str, Any]) -> Any:
        messages = _chat_messages(messages, options)
        model_spec, client_kwargs = _model_spec(self.context, spec)
        async with republic.get_model(model_spec, **client_kwargs) as model:
            response = await model.chat(messages, **options)
        return _encode_response(response)

    async def _stream_open(self, spec: str | None, messages: list[Any], options: dict[str, Any]) -> Any:
        messages = _chat_messages(messages, options)
        model_spec, client_kwargs = _model_spec(self.context, spec)
        async with AsyncExitStack() as stack:
            model = await stack.enter_async_context(republic.get_model(model_spec, **client_kwargs))
            stream = await stack.enter_async_context(model.stream(messages, **options))
            stream_id = uuid.uuid4().hex
            self._streams[stream_id] = _StreamPump(stack.pop_all(), stream)
        return {"stream": stream_id}

    async def _stream_next(self, stream: str) -> Any:
        pump = self._streams.get(stream)
        if pump is None:
            raise BubError(ErrorKind.INVALID_INPUT, "The stream is closed.")
        result = await pump.next()
        if result["done"]:
            await self._stream_close(stream)
        return result

    async def _stream_close(self, stream: str) -> None:
        if (pump := self._streams.pop(stream, None)) is not None:
            await pump.aclose()

    async def _embed(self, spec: str, texts: list[str], dimensions: int | None = None) -> Any:
        model_spec, client_kwargs = _model_spec(self.context, spec)
        async with republic.get_embedding_model(model_spec, **client_kwargs) as model:
            response = await model.embed_many(texts, dimensions=dimensions)
        return {"vectors": response.vectors, "model": response.model, "token_usage": asdict(response.token_usage)}

    async def _decide(self, spec: str, state: Any, questions: dict[str, Question]) -> Any:
        if not all(isinstance(question, Noul | Choice | Score) for question in questions.values()):
            raise BubError(ErrorKind.INVALID_INPUT, "decide() takes republic.decisions questions.")
        model_spec, client_kwargs = _model_spec(self.context, spec)
        async with republic.get_decision_model(model_spec, **client_kwargs) as model:
            response = await model.decide(state, questions=questions)
        return {
            "answers": {key: asdict(answer) for key, answer in response.answers.items()},
            "model": response.model,
            "token_usage": asdict(response.token_usage),
        }
