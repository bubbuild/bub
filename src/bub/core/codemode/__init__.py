"""Code mode: let the model call tools from Python it writes, through one ``run_code`` tool.

:class:`CodeMode` bundles what an agent needs: the ``run_code`` tool, a system prompt
section describing the code-callable tools as a Python stub, and the ``attach_*`` media
tools. It works with a bare :class:`bub.core.Agent`::

    code_mode = CodeMode([search, fetch], models="openai:gpt-5")
    agent = Agent(model=model, tools=[code_mode.run_code], system_prompt=["You are ...", code_mode.prompt])

Code runs in an :class:`bub.environment.Environment`, a Python process on the host by
default. Calls the code makes go through the running agent's tool events, so hooks and
handlers see them with ``context.code_mode`` set.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import mimetypes
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import republic

from bub.core.codemode.republic_bridge import (
    REPUBLIC_CALL_PREFIX,
    ModelResolver,
    RepublicSession,
    decode_value,
    default_model_resolver,
    encode_value,
)
from bub.core.codemode.stub import _identifier, render_tool_stub
from bub.core.system_prompt import PromptContext
from bub.core.tools import Tool, ToolContext, ToolExecutor, content_result, model_tools
from bub.environment import CallTool, CodeFailed, Environment, LocalEnvironment
from bub.errors import BubError, ErrorKind
from bub.prompt import UserContent
from bub.turn import TurnState

__all__ = [
    "DEFAULT_RUN_CODE_TIMEOUT_SECONDS",
    "MEDIA_TOOLS",
    "RUN_CODE_TOOL_NAME",
    "CodeMode",
    "ModelResolver",
    "attach_audio",
    "attach_image",
    "attach_video",
    "default_model_resolver",
    "render_code_mode_prompt",
    "render_tool_stub",
    "write_stub",
]

RUN_CODE_TOOL_NAME = "run_code"
DEFAULT_RUN_CODE_TIMEOUT_SECONDS = 120

CodeTools = Iterable[Tool] | Callable[[TurnState], Iterable[Tool] | None]
"""The tools callable from code: fixed, or chosen per run from the turn state (None disables ``run_code``)."""


@dataclass(frozen=True)
class _CodeRun:
    environment: Environment
    media: list[UserContent]


_ACTIVE_RUN: contextvars.ContextVar[_CodeRun | None] = contextvars.ContextVar("bub_run_code", default=None)


class CodeMode:
    """The ``run_code`` tool and the system prompt section for a set of code-callable tools.

    Args:
        tools: Tools callable from code as ``tools.<name>(...)``. A callable receives the
            turn state and returns the tools for that run, or None when code mode is off.
        environment: Where the code runs, or a callable returning it for the turn state.
            Defaults to a :class:`bub.environment.LocalEnvironment` in the working directory.
        models: What ``republic.get_*model(spec)`` in the code resolves to: a default
            ``provider:model`` spec for ``get_model()``, or a :data:`ModelResolver`. By default
            specs must be explicit and clients read credentials from the environment.
        stub_dir: Write the tool stub to a file in this directory (or the one returned for
            the turn state) and point the prompt at it, for agents that can read files.
            None puts the stub in the system prompt itself.
        media_tools: Also offer ``attach_image``, ``attach_audio`` and ``attach_video`` to
            the code, which attach media to the ``run_code`` result.
        name: Name of the ``run_code`` tool.
    """

    def __init__(
        self,
        tools: CodeTools = (),
        *,
        environment: Environment | Callable[[TurnState], Environment] | None = None,
        models: str | ModelResolver | None = None,
        stub_dir: Path | Callable[[TurnState], Path] | None = None,
        media_tools: bool = True,
        name: str = RUN_CODE_TOOL_NAME,
    ) -> None:
        self._tools = tools if callable(tools) else list(tools)
        self._environment = LocalEnvironment() if environment is None else environment
        self.resolve_model = models if callable(models) else default_model_resolver(models)
        self.stub_dir = stub_dir
        self.media_tools = media_tools
        self.run_code: Tool = Tool.from_callable(
            self._run_code, name=name, description=_RUN_CODE_DESCRIPTION, context=True, exposure="direct"
        )
        """The model-facing tool; add it to the agent's tools."""

    def code_tools(self, state: TurnState) -> list[Tool] | None:
        """Return the model-safe tools callable from code in this run, or None when code mode is off."""
        tools = self._tools(state) if callable(self._tools) else self._tools
        if tools is None:
            return None
        result = model_tools(tools)
        if self.media_tools:
            names = {tool_item.name for tool_item in result}
            result.extend(tool_item for tool_item in MEDIA_TOOLS if tool_item.name not in names)
        return result

    def environment(self, state: TurnState) -> Environment:
        """Return the environment the code runs in for this run."""
        return self._environment(state) if callable(self._environment) else self._environment

    def prompt(self, context: PromptContext) -> str | None:
        """System prompt section describing the code-callable tools; add it to the agent's system prompt."""
        state = context.state
        if (tools := self.code_tools(state)) is None:
            return None
        stub = render_tool_stub(tools)
        if self.stub_dir is not None:
            directory = self.stub_dir(state) if callable(self.stub_dir) else self.stub_dir
            return render_code_mode_prompt(write_stub(stub, directory), tool_name=self.run_code.name)
        return render_code_mode_prompt(stub, tool_name=self.run_code.name)

    async def _run_code(
        self, code: str, timeout_seconds: int = DEFAULT_RUN_CODE_TIMEOUT_SECONDS, *, context: ToolContext
    ) -> str | dict[str, Any]:
        code_tools = self.code_tools(context.state)
        if code_tools is None:
            raise BubError(ErrorKind.INVALID_INPUT, "Code mode is not enabled for this run.")
        tools_by_name = {_identifier(item.name): item for item in code_tools}
        code_context = replace(context, code_mode=True)
        republic_session = RepublicSession(code_context, self.resolve_model)
        call_tool = _code_tool_caller(tools_by_name, code_context, republic_session)
        environment = self.environment(context.state)

        output: list[str] = []
        media: list[UserContent] = []
        token = _ACTIVE_RUN.set(_CodeRun(environment, media))
        try:
            async with asyncio.timeout(timeout_seconds) as deadline:
                await environment.run_code(code, tools=list(tools_by_name), call_tool=call_tool, write=output.append)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise BubError(
                ErrorKind.TOOL, f"Code timed out after {timeout_seconds} seconds", details={"output": "".join(output)}
            ) from None
        except CodeFailed as exc:
            raise BubError(
                ErrorKind.TOOL,
                f"Code raised {exc.error}",
                details={"output": "".join(output), "traceback": exc.traceback},
            ) from exc
        except BubError as exc:
            raise BubError(exc.kind, exc.message, details={"output": "".join(output), **(exc.details or {})}) from exc
        finally:
            _ACTIVE_RUN.reset(token)
            await republic_session.aclose()
        if media:
            return content_result(["".join(output), *media] if output else media)
        return "".join(output)


_RUN_CODE_DESCRIPTION = """Run Python code in the environment and return everything it prints.

Tools are async functions available as `tools.<name>(...)`: await them with keyword arguments
(top-level `await` is allowed). See the tool stub referenced in the system prompt for their
signatures and result types. The `republic` module provides media loaders and models. Media passed
to `tools.attach_image/audio/video`, as an object, path or URL, is attached to the result. The code is stopped after timeout_seconds."""


def write_stub(content: str, directory: Path) -> Path:
    """Write a stub into ``directory``; the file name depends only on the content, so unchanged stubs keep their path."""
    path = directory / f"tools-{hashlib.sha256(content.encode()).hexdigest()[:12]}.pyi"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
    return path


def render_code_mode_prompt(stub: Path | str, *, tool_name: str = RUN_CODE_TOOL_NAME) -> str:
    """Render the code mode prompt for a stub file path, or for the stub text itself."""
    if isinstance(stub, Path):
        where = f"in the stub file: {stub}\nRead the stub before calling a tool you have not used yet. "
    else:
        where = f"in this stub:\n```python\n{stub.rstrip()}\n```\n"
    return (
        "<code_mode>\n"
        f"More tools are available as async Python functions `tools.<name>(...)` inside `{tool_name}`. "
        f"Their signatures, result types and documentation are {where}"
        "Always `await` tool calls (top-level `await` "
        "is allowed) and pass keyword arguments; they return structured values and raise on failure. "
        f"`{tool_name}` returns only what the code prints, so print the results you need, and combine "
        "several tool calls in one run when possible. To look at an image, audio or video, pass its path, URL "
        "or `republic` media object to `tools.attach_image(image=...)`, `tools.attach_audio(audio=...)` or "
        f"`tools.attach_video(video=...)`: it is attached to the `{tool_name}` result.\n"
        "The code can also use the `republic` module (see the end of the stub) to call models with "
        "`republic.get_model()`, "
        "`republic.get_embedding_model(spec)` and `republic.get_decision_model(spec)`; `get_model()` without a "
        "spec is the default model of this session.\n"
        "</code_mode>"
    )


def _code_tool_caller(
    tools_by_name: Mapping[str, Tool], context: ToolContext, republic_session: RepublicSession
) -> CallTool:
    """Serve the code's ``tools.<name>`` and ``republic`` calls."""
    # Code consumes structured results, so this executor does not render them to text.
    executor = ToolExecutor(context.events, render=False)

    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        arguments = decode_value(arguments)
        if name.startswith(REPUBLIC_CALL_PREFIX):
            return encode_value(await republic_session.call(name, arguments))
        tool_item = tools_by_name.get(name)
        if tool_item is None:
            raise BubError(ErrorKind.INVALID_INPUT, f"unknown tool: tools.{name}")
        return encode_value(await executor.execute(tool_item, arguments, context=context))

    return call_tool


_REMOTE_MEDIA_PREFIXES = ("http://", "https://", "gs://", "data:")
_MEDIA_CLASSES: dict[str, type[republic.Image | republic.Audio | republic.Video]] = {
    "image": republic.Image,
    "audio": republic.Audio,
    "video": republic.Video,
}


async def _load_media(
    kind: str, source: str, environment: Environment
) -> republic.Image | republic.Audio | republic.Video:
    """Load media from a URL, a data URL, or a file path in the environment."""
    if source.startswith(_REMOTE_MEDIA_PREFIXES):
        media: republic.Image | republic.Audio | republic.Video = getattr(republic, kind)(source)
        return media
    media_type, _ = mimetypes.guess_type(source)
    if media_type is None:
        raise ValueError(f"cannot guess the media type of {source!r}")
    data = await environment.read_bytes(environment.resolve_path(source))
    return _MEDIA_CLASSES[kind](media_type, data=data)


async def _attach(kind: str, item: republic.Image | republic.Audio | republic.Video | str) -> str:
    run = _ACTIVE_RUN.get()
    if run is None:
        raise BubError(ErrorKind.INVALID_INPUT, f"tools.attach_{kind} can only be called from {RUN_CODE_TOOL_NAME}.")
    if isinstance(item, str):
        try:
            item = await _load_media(kind, item, run.environment)
        except (OSError, ValueError) as exc:
            raise BubError(ErrorKind.INVALID_INPUT, f"Cannot load the {kind}: {exc}") from exc
    run.media.append(item)
    return f"The {kind} is attached to the {RUN_CODE_TOOL_NAME} result."


async def _attach_image(image: republic.Image | str, *, context: ToolContext) -> str:
    """Attach an image to the result of this `run_code` call, so you can see it.

    `image` is a `republic.Image`, a file path in the environment, an http(s) URL, or a data URL.
    """
    return await _attach("image", image)


async def _attach_audio(audio: republic.Audio | str, *, context: ToolContext) -> str:
    """Attach audio to the result of this `run_code` call, so you can hear it.

    `audio` is a `republic.Audio`, a file path in the environment, an http(s) URL, or a data URL.
    """
    return await _attach("audio", audio)


async def _attach_video(video: republic.Video | str, *, context: ToolContext) -> str:
    """Attach a video to the result of this `run_code` call, so you can watch it.

    `video` is a `republic.Video`, a file path in the environment, an http(s) URL, or a data URL.
    """
    return await _attach("video", video)


attach_image = Tool.from_callable(_attach_image, name="attach_image", context=True, exposure="code")
attach_audio = Tool.from_callable(_attach_audio, name="attach_audio", context=True, exposure="code")
attach_video = Tool.from_callable(_attach_video, name="attach_video", context=True, exposure="code")
MEDIA_TOOLS = (attach_image, attach_audio, attach_video)
"""Code-only tools attaching media to the ``run_code`` result; they work only inside ``run_code``."""
