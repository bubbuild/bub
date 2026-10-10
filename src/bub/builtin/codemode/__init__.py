"""Code mode: expose tools to model-written Python through a generated stub and ``run_code``."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import keyword
import mimetypes
import re
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

import republic

from bub.builtin.codemode.republic_bridge import REPUBLIC_CALL_PREFIX, RepublicSession, decode_value, encode_value
from bub.builtin.environment import environment_from_state
from bub.builtin.settings import set_session_setting
from bub.environment import CallTool, CodeFailed
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import AgentHooks
from bub.prompt import UserContent
from bub.tools import Tool, ToolContext, ToolExecutor, content_result, model_tools, tool

RUN_CODE_TOOL_NAME = "run_code"
CODE_MODE_STATE_KEY = "code_mode"
CODE_TOOLS_STATE_KEY = "_runtime_code_tools"
DEFAULT_RUN_CODE_TIMEOUT_SECONDS = 120

_RUN_CODE_MEDIA: contextvars.ContextVar[list[UserContent] | None] = contextvars.ContextVar(
    "bub_run_code_media", default=None
)

_STUB_HEADER = '''"""Bub tools available inside `run_code` as `tools.<name>(...)`.

Every tool is an async function: `await` it (top-level `await` is allowed) and pass keyword
arguments only. Each call returns the structured value described by its return type and
raises an exception when the tool fails. Use `asyncio.gather` to run independent calls
concurrently.

The `republic` module is also available (preset as a global and importable); it is documented
at the end of this file.
"""

from typing import Any, Literal, NotRequired, TypedDict

import republic'''

_REPUBLIC_STUB = """# ---------------------------------------------------------------------------
# `republic`: media and models inside `run_code`. Models are served by Bub with its
# credentials and settings; every model method is async. Only the members below exist.
#
# class republic.Image / republic.Audio / republic.Video:
#     __init__(media_type: str, data: bytes | None = None, url: str | None = None)
#     media_type: str; data: bytes | None; url: str | None
#     base64_data: str; data_url: str
#     `tools.attach_image/audio/video` also take a file path in the environment, an http(s)/gs URL
#     or a data URL instead, and load it for you.
#
# republic.system(text: str) -> Message
# republic.user(*content: str | Image | Audio | Video) -> Message
# republic.assistant(*content: str | Image | Audio | Video) -> Message
#     Message has .role, .content (list of text and media) and .text.
#
# republic.get_model(spec: str | None = None) -> ChatModel
#     A "provider:model" spec; None is the model running this session.
#     await model.chat(prompt: str | Message | list[str | Message], **options) -> response
#         options: max_tokens, temperature, top_p, top_k, stop, seed, reasoning_effort, extra_body...
#         (no tools). A plain string is a user message.
#         response.text, .reasoning, .refusal, .finish_reason, .model,
#         .token_usage.input_tokens / .output_tokens, .message (append it to continue the conversation)
#     model.stream(prompt, **options) -> Stream   # same arguments as chat(); not awaited
#         async with model.stream(...) as stream:
#             async for event in stream: ...
#         Events from republic.events: TextDelta(.chunk), ReasoningDelta(.chunk), RefusalDelta(.chunk),
#         UsageDelta(.usage), ImageReady(.image), CitationAdded(.citation), Completed(.response, last).
#         After iterating: stream.response (as returned by chat()), stream.text, .reasoning, .token_usage.
# republic.get_embedding_model(spec: str) -> EmbeddingModel
#     await model.embed(text: str, *, dimensions: int | None = None) -> response   # response.vector
#     await model.embed_many(texts: list[str], *, dimensions: int | None = None) -> response   # response.vectors
# republic.get_decision_model(spec: str) -> DecisionModel
#     await model.decide(state, *, questions: dict[str, Question]) -> response
#         Questions from republic.decisions: Noul(instructions, criteria=None) (yes/no),
#         Choice(instructions, criteria) (pick one), Score(instructions, criteria) (ordered levels).
#         response.answers[id] or response.<id>: .noul | .choice/.probabilities/.confidence | .score/...
#
# Example:
#     model = republic.get_model()
#     chart = republic.Image("image/png", data=open("chart.png", "rb").read())
#     reply = await model.chat([republic.user("Describe this chart.", chart)])
#     print(reply.text)
#     async with model.stream("Write a haiku.") as stream:
#         async for event in stream:
#             if isinstance(event, republic.events.TextDelta):
#                 print(event.chunk, end="")"""
_JSON_TYPES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "null": "None"}


def _identifier(name: str) -> str:
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit() or keyword.iskeyword(ident):
        ident = f"_{ident}"
    return ident


def _is_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^0-9A-Za-z]+", name))


_MEDIA_FIELDS = {"media_type", "data", "url"}


def _media_class(schema: dict[str, Any]) -> str | None:
    """Recognize the JSON schema of ``republic.Image``, ``Audio`` and ``Video`` parameters."""
    title = schema.get("title")
    properties = schema.get("properties")
    if title in ("Image", "Audio", "Video") and isinstance(properties, dict) and set(properties) == _MEDIA_FIELDS:
        return str(title)
    return None


def _docstring(text: str, indent: str = "    ") -> list[str]:
    text = text.strip().replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if not text:
        return []
    lines = text.splitlines()
    if len(lines) == 1:
        return [f'{indent}"""{lines[0]}"""']
    return [f'{indent}"""{lines[0]}', *(f"{indent}{line}".rstrip() for line in lines[1:]), f'{indent}"""']


class _StubBuilder:
    """Render tool JSON schemas as Python stub declarations."""

    def __init__(self) -> None:
        self.classes: dict[str, str] = {}
        self._refs: dict[tuple[int, str], str] = {}

    def function(self, tool_item: Tool) -> str:
        name = _identifier(tool_item.name)
        schema = tool_item.parameters or {}
        defs = self._defs(schema)
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        params: list[str] = []
        arg_docs: list[str] = []
        for key, prop in properties.items():
            hint = self.type_hint(prop, defs, f"{_pascal(name)}{_pascal(key)}")
            if key in required:
                params.append(f"{key}: {hint}")
            elif isinstance(prop, dict) and "default" in prop:
                params.append(f"{key}: {hint} = {prop['default']!r}")
            else:
                params.append(f"{key}: {hint} = ...")
            description = prop.get("description") if isinstance(prop, dict) else None
            if description:
                arg_docs.append(f"    {key}: {' '.join(description.split())}")
        if not all(_is_identifier(key) for key in properties):
            signature = "**kwargs: Any"
        elif params:
            signature = f"*, {', '.join(params)}"
        else:
            signature = ""

        output = tool_item.output_schema
        returns = "Any" if output is None else self.type_hint(output, self._defs(output), f"{_pascal(name)}Result")
        doc = tool_item.description.strip()
        if arg_docs:
            doc = f"{doc}\n\nArgs:\n" + "\n".join(arg_docs) if doc else "Args:\n" + "\n".join(arg_docs)
        body = _docstring(doc) or ["    ..."]
        return "\n".join([f"async def {name}({signature}) -> {returns}:", *body])

    def type_hint(  # noqa: C901
        self, schema: Any, defs: dict[str, Any], name_hint: str, ref_key: tuple[int, str] | None = None
    ) -> str:
        if not isinstance(schema, dict) or not schema:
            return "Any"
        if (media := _media_class(schema)) is not None:
            return f"republic.{media}"
        if local_defs := self._defs(schema):
            # Schemas of single parameters carry their own definitions.
            defs = {**defs, **local_defs}
        if isinstance(ref := schema.get("$ref"), str):
            return self._ref(ref, defs)
        for key in ("anyOf", "oneOf"):
            if isinstance(options := schema.get(key), list):
                return self._union(self.type_hint(option, defs, name_hint) for option in options)
        if isinstance(all_of := schema.get("allOf"), list) and len(all_of) == 1:
            return self.type_hint(all_of[0], defs, name_hint, ref_key)
        if "const" in schema:
            return f"Literal[{schema['const']!r}]"
        if isinstance(enum := schema.get("enum"), list) and enum:
            return f"Literal[{', '.join(repr(value) for value in enum)}]"

        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            return self._union(self.type_hint({**schema, "type": item}, defs, name_hint) for item in schema_type)
        if schema_type == "array":
            if isinstance(prefix := schema.get("prefixItems"), list):
                return f"tuple[{', '.join(self.type_hint(item, defs, name_hint) for item in prefix)}]"
            return f"list[{self.type_hint(schema.get('items'), defs, f'{name_hint}Item')}]"
        if schema_type == "object":
            if isinstance(properties := schema.get("properties"), dict) and properties:
                return self._typed_dict(schema, properties, defs, schema.get("title") or name_hint, ref_key)
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict) and extra:
                return f"dict[str, {self.type_hint(extra, defs, f'{name_hint}Value')}]"
            return "dict[str, Any]"
        return _JSON_TYPES.get(schema_type, "Any") if isinstance(schema_type, str) else "Any"

    @staticmethod
    def _defs(schema: dict[str, Any]) -> dict[str, Any]:
        defs = schema.get("$defs", schema.get("definitions"))
        return defs if isinstance(defs, dict) else {}

    @staticmethod
    def _union(hints: Iterable[str]) -> str:
        unique = list(dict.fromkeys(hints))
        return "Any" if "Any" in unique else " | ".join(unique)

    def _ref(self, ref: str, defs: dict[str, Any]) -> str:
        name = ref.rsplit("/", 1)[-1]
        key = (id(defs), name)
        if key in self._refs:
            return self._refs[key]
        target = defs.get(name)
        if not isinstance(target, dict):
            return "Any"
        hint = self.type_hint(target, defs, name, ref_key=key)
        self._refs[key] = hint
        return hint

    def _typed_dict(
        self,
        schema: dict[str, Any],
        properties: dict[str, Any],
        defs: dict[str, Any],
        title: str,
        ref_key: tuple[int, str] | None,
    ) -> str:
        base = _identifier(_pascal(title) or "Result")
        class_name = base
        index = 2
        while class_name in self.classes:
            class_name = f"{base}{index}"
            index += 1
        # Reserve the name first so recursive references resolve to this class.
        self.classes[class_name] = ""
        if ref_key is not None:
            self._refs[ref_key] = class_name

        required = set(schema.get("required") or [])
        fields: list[tuple[str, str, str | None]] = []
        for key, prop in properties.items():
            hint = self.type_hint(prop, defs, f"{class_name}{_pascal(key)}")
            if key not in required:
                hint = f"NotRequired[{hint}]"
            description = prop.get("description") if isinstance(prop, dict) else None
            fields.append((key, hint, description))
        source = self._class_source(class_name, fields, schema.get("description"))

        # Reuse an identical class that was declared under the same title by another tool.
        for existing, existing_source in self.classes.items():
            if (
                existing != class_name
                and re.fullmatch(rf"{re.escape(base)}\d*", existing)
                and existing_source == source.replace(class_name, existing)
            ):
                del self.classes[class_name]
                if ref_key is not None:
                    self._refs[ref_key] = existing
                return existing
        self.classes[class_name] = source
        return class_name

    @staticmethod
    def _class_source(name: str, fields: list[tuple[str, str, str | None]], description: object) -> str:
        if not all(_is_identifier(key) for key, _, _ in fields):
            items = ", ".join(f"{key!r}: {hint}" for key, hint, _ in fields)
            return f"{name} = TypedDict({name!r}, {{{items}}})"
        lines = [f"class {name}(TypedDict):"]
        if isinstance(description, str):
            lines.extend(_docstring(description))
        for key, hint, field_description in fields:
            if field_description:
                lines.append(f"    # {' '.join(field_description.split())}")
            lines.append(f"    {key}: {hint}")
        return "\n".join(lines)


def render_tool_stub(tools: Iterable[Tool]) -> str:
    """Render a Python stub declaring one function per model-facing tool."""
    builder = _StubBuilder()
    functions = [builder.function(tool_item) for tool_item in model_tools(tools)]
    return "\n\n\n".join([_STUB_HEADER, *builder.classes.values(), *functions, _REPUBLIC_STUB]) + "\n"


def write_tool_stub(tools: Iterable[Tool], *, session_id: str, workspace: Path) -> Path:
    """Write the tool stub under Bub home; the path stays stable while the session's tool set is unchanged."""
    import bub

    content = render_tool_stub(tools)
    session_key = hashlib.sha256(f"{workspace}\0{session_id}".encode()).hexdigest()[:16]
    content_key = hashlib.sha256(content.encode()).hexdigest()[:12]
    path = bub.home / "codemode" / session_key / f"tools-{content_key}.pyi"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    return path


def render_code_mode_prompt(stub_path: Path) -> str:
    return (
        "<code_mode>\n"
        f"More tools are available as async Python functions `tools.<name>(...)` inside `{RUN_CODE_TOOL_NAME}`. "
        f"Their signatures, result types and documentation are in the stub file: {stub_path}\n"
        "Read the stub before calling a tool you have not used yet. Always `await` tool calls (top-level `await` "
        "is allowed) and pass keyword arguments; they return structured values and raise on failure. "
        f"`{RUN_CODE_TOOL_NAME}` returns only what the code prints, so print the results you need, and combine "
        "several tool calls in one run when possible. To look at an image, audio or video, pass its path, URL "
        "or `republic` media object to `tools.attach_image(image=...)`, `tools.attach_audio(audio=...)` or "
        f"`tools.attach_video(video=...)`: it is attached to the `{RUN_CODE_TOOL_NAME}` result.\n"
        "The code can also use the `republic` module (see the end of the stub) to call models with "
        "`republic.get_model()`, "
        "`republic.get_embedding_model(spec)` and `republic.get_decision_model(spec)`; `get_model()` without a "
        "spec is the model running this session.\n"
        "</code_mode>"
    )


def _code_tool_caller(
    tools_by_name: dict[str, Tool], context: ToolContext, republic_session: RepublicSession
) -> CallTool:
    """Serve the code's ``tools.<name>`` and ``republic`` calls."""
    agent = context.state.get("_runtime_agent")
    hooks: AgentHooks | None = getattr(getattr(agent, "model_runner", None), "hooks", None)
    # Code consumes structured results, so this executor does not render them to text.
    executor = ToolExecutor(hooks=hooks, render=False)
    code_context = replace(context, code_mode=True)

    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        arguments = decode_value(arguments)
        if name.startswith(REPUBLIC_CALL_PREFIX):
            return encode_value(await republic_session.call(name, arguments))
        tool_item = tools_by_name.get(name)
        if tool_item is None:
            raise BubError(ErrorKind.INVALID_INPUT, f"unknown tool: tools.{name}")
        execution = await executor.execute_async([(tool_item, arguments)], context=code_context)
        if execution.error is not None:
            raise execution.error
        return encode_value(execution.tool_results[0])

    return call_tool


@tool(name=RUN_CODE_TOOL_NAME, context=True, exposure="direct")
async def run_code(
    code: str, timeout_seconds: int = DEFAULT_RUN_CODE_TIMEOUT_SECONDS, *, context: ToolContext
) -> str | dict[str, Any]:
    """Run Python code in the environment and return everything it prints.

    Tools are async functions available as `tools.<name>(...)`: await them with keyword arguments
    (top-level `await` is allowed). See the tool stub file referenced in the system prompt for their
    signatures and result types. The `republic` module provides media loaders and models. Media passed
    to `tools.attach_image/audio/video`, as an object, path or URL, is attached to the result. The code is stopped after timeout_seconds.
    """
    code_tools = context.state.get(CODE_TOOLS_STATE_KEY)
    if code_tools is None:
        raise BubError(ErrorKind.INVALID_INPUT, "Code mode is not enabled for this run.")
    tools_by_name = {_identifier(item.name): item for item in code_tools}
    republic_session = RepublicSession(replace(context, code_mode=True))
    call_tool = _code_tool_caller(tools_by_name, context, republic_session)

    output: list[str] = []
    media: list[UserContent] = []
    token = _RUN_CODE_MEDIA.set(media)
    try:
        async with asyncio.timeout(timeout_seconds) as deadline:
            await environment_from_state(context.state).run_code(
                code, tools=list(tools_by_name), call_tool=call_tool, write=output.append
            )
    except TimeoutError:
        if not deadline.expired():
            raise
        raise BubError(
            ErrorKind.TOOL, f"Code timed out after {timeout_seconds} seconds", details={"output": "".join(output)}
        ) from None
    except CodeFailed as exc:
        raise BubError(
            ErrorKind.TOOL, f"Code raised {exc.error}", details={"output": "".join(output), "traceback": exc.traceback}
        ) from exc
    except BubError as exc:
        raise BubError(exc.kind, exc.message, details={"output": "".join(output), **(exc.details or {})}) from exc
    finally:
        _RUN_CODE_MEDIA.reset(token)
        await republic_session.aclose()
    if media:
        return content_result(["".join(output), *media] if output else media)
    return "".join(output)


_REMOTE_MEDIA_PREFIXES = ("http://", "https://", "gs://", "data:")
_MEDIA_CLASSES: dict[str, type[republic.Image | republic.Audio | republic.Video]] = {
    "image": republic.Image,
    "audio": republic.Audio,
    "video": republic.Video,
}


async def _load_media(kind: str, source: str, context: ToolContext) -> republic.Image | republic.Audio | republic.Video:
    """Load media from a URL, a data URL, or a file path in the session environment."""
    if source.startswith(_REMOTE_MEDIA_PREFIXES):
        media: republic.Image | republic.Audio | republic.Video = getattr(republic, kind)(source)
        return media
    media_type, _ = mimetypes.guess_type(source)
    if media_type is None:
        raise ValueError(f"cannot guess the media type of {source!r}")
    environment = environment_from_state(context.state)
    data = await environment.read_bytes(environment.resolve_path(source))
    return _MEDIA_CLASSES[kind](media_type, data=data)


async def _attach(kind: str, item: republic.Image | republic.Audio | republic.Video | str, context: ToolContext) -> str:
    media = _RUN_CODE_MEDIA.get()
    if media is None:
        raise BubError(ErrorKind.INVALID_INPUT, f"tools.attach_{kind} can only be called from {RUN_CODE_TOOL_NAME}.")
    if isinstance(item, str):
        try:
            item = await _load_media(kind, item, context)
        except (OSError, ValueError) as exc:
            raise BubError(ErrorKind.INVALID_INPUT, f"Cannot load the {kind}: {exc}") from exc
    media.append(item)
    return f"The {kind} is attached to the {RUN_CODE_TOOL_NAME} result."


@tool(name="attach_image", context=True, exposure="code")
async def attach_image(image: republic.Image | str, *, context: ToolContext) -> str:
    """Attach an image to the result of this `run_code` call, so you can see it.

    `image` is a `republic.Image`, a file path in the environment, an http(s) URL, or a data URL.
    """
    return await _attach("image", image, context)


@tool(name="attach_audio", context=True, exposure="code")
async def attach_audio(audio: republic.Audio | str, *, context: ToolContext) -> str:
    """Attach audio to the result of this `run_code` call, so you can hear it.

    `audio` is a `republic.Audio`, a file path in the environment, an http(s) URL, or a data URL.
    """
    return await _attach("audio", audio, context)


@tool(name="attach_video", context=True, exposure="code")
async def attach_video(video: republic.Video | str, *, context: ToolContext) -> str:
    """Attach a video to the result of this `run_code` call, so you can watch it.

    `video` is a `republic.Video`, a file path in the environment, an http(s) URL, or a data URL.
    """
    return await _attach("video", video, context)


@tool(name="code_mode", context=True, exposure="command")
async def set_code_mode(enable: bool, *, context: ToolContext) -> str:
    """Enable or disable code mode for THIS session. Invoke as the `,code_mode enable=true` command.

    In code mode the model calls `direct` tools directly and every other tool from
    Python through `run_code`. Takes effect on the NEXT turn and persists across restarts.
    """
    await set_session_setting(context, CODE_MODE_STATE_KEY, enable)
    return f"Session code mode {'enabled' if enable else 'disabled'} (applies from the next turn)."
