"""Code mode: expose tools to model-written Python through a generated stub and ``run_code``."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import keyword
import re
import uuid
from collections.abc import AsyncGenerator, Iterable
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from bub.builtin import codemode_runner
from bub.builtin.environment import environment_from_state
from bub.builtin.settings import set_session_setting
from bub.environment import Process
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import AgentHooks
from bub.tools import Tool, ToolContext, ToolExecutor, model_tools, tool

RUN_CODE_TOOL_NAME = "run_code"
CODE_MODE_STATE_KEY = "code_mode"
CODE_TOOLS_STATE_KEY = "_runtime_code_tools"
DEFAULT_RUN_CODE_TIMEOUT_SECONDS = 120
_STOP_TIMEOUT_SECONDS = 3.0
_RUNNER_SOURCE = Path(codemode_runner.__file__).read_text(encoding="utf-8")

_STUB_HEADER = '''"""Bub tools available inside `run_code` as `tools.<name>(...)`.

Every tool is an async function: `await` it (top-level `await` is allowed) and pass keyword
arguments only. Each call returns the structured value described by its return type and
raises an exception when the tool fails. Use `asyncio.gather` to run independent calls
concurrently.
"""

from typing import Any, Literal, NotRequired, TypedDict'''
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
    return "\n\n\n".join([_STUB_HEADER, *builder.classes.values(), *functions]) + "\n"


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
        "several tool calls in one run when possible.\n"
        "</code_mode>"
    )


class _CodeRunner:
    """Drive one runner process: send the code, serve its tool calls, and collect the outcome."""

    def __init__(self, process: Process, tools: dict[str, Tool], executor: ToolExecutor, context: ToolContext):
        self.process = process
        self.tools = tools
        self.executor = executor
        self.context = context
        self.output: list[str] = []
        self._write_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._write_lock:
            await self.process.write_stdin(json.dumps(message).encode() + b"\n")

    async def run(self, code: str, filename: str) -> dict[str, Any]:
        await self.send({"type": "run", "code": code, "filename": filename, "tools": list(self.tools)})
        calls: set[asyncio.Task[None]] = set()
        try:
            async with aclosing(_read_lines(self.process.stdout)) as lines:
                async for line in lines:
                    message = _parse_message(line)
                    if message is None:
                        # Text the code wrote around the protocol, e.g. to sys.__stdout__.
                        self.output.append(line.decode("utf-8", errors="replace") + "\n")
                    elif message.get("type") == "output":
                        self.output.append(str(message.get("data", "")))
                    elif message.get("type") == "call":
                        task = asyncio.create_task(self._serve_call(message))
                        calls.add(task)
                        task.add_done_callback(calls.discard)
                    elif message.get("type") in ("done", "failed"):
                        return message
        finally:
            for task in calls:
                task.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
        raise EOFError("code runner exited before reporting a result")

    async def _serve_call(self, message: dict[str, Any]) -> None:
        call_id, name, arguments = message.get("id"), message.get("name"), message.get("arguments")
        tool_item = self.tools.get(name) if isinstance(name, str) else None
        if tool_item is None or not isinstance(arguments, dict):
            reply: dict[str, Any] = {"type": "error", "id": call_id, "message": f"unknown tool call: {name!r}"}
        else:
            execution = await self.executor.execute_async([(tool_item, arguments)], context=self.context)
            if execution.error is not None:
                reply = {"type": "error", "id": call_id, "message": str(execution.error)}
            else:
                try:
                    value = to_jsonable_python(execution.tool_results[0])
                except (TypeError, ValueError) as exc:
                    reply = {"type": "error", "id": call_id, "message": f"tool result is not JSON serializable: {exc}"}
                else:
                    reply = {"type": "result", "id": call_id, "value": value}
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            # If the runner already exited, run() reports that.
            await self.send(reply)


async def _read_lines(stream: asyncio.StreamReader) -> AsyncGenerator[bytes]:
    """Split a stream into lines without the StreamReader line-length limit; results can be large."""
    buffer = bytearray()
    while chunk := await stream.read(1 << 16):
        buffer += chunk
        while (index := buffer.find(b"\n")) >= 0:
            yield bytes(buffer[:index])
            del buffer[: index + 1]
    if buffer:
        yield bytes(buffer)


def _parse_message(line: bytes) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) and isinstance(message.get("type"), str) else None


async def _stop(process: Process) -> None:
    """Kill the runner and anything the code started, and reap it."""
    process.signal(kill=True)
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_STOP_TIMEOUT_SECONDS):
            await process.wait()


@tool(name=RUN_CODE_TOOL_NAME, context=True, preserve=True)
async def run_code(code: str, timeout_seconds: int = DEFAULT_RUN_CODE_TIMEOUT_SECONDS, *, context: ToolContext) -> str:
    """Run Python code in a separate process and return everything it prints.

    Tools are async functions available as `tools.<name>(...)`: await them with keyword arguments
    (top-level `await` is allowed). See the tool stub file referenced in the system prompt for their
    signatures and result types. The process is killed after timeout_seconds.
    """
    code_tools = context.state.get(CODE_TOOLS_STATE_KEY)
    if code_tools is None:
        raise BubError(ErrorKind.INVALID_INPUT, "Code mode is not enabled for this run.")
    agent = context.state.get("_runtime_agent")
    hooks: AgentHooks | None = getattr(getattr(agent, "model_runner", None), "hooks", None)
    environment = environment_from_state(context.state)
    process = await environment.spawn([environment.python, "-u", "-c", _RUNNER_SOURCE])
    runner = _CodeRunner(
        process,
        {_identifier(item.name): item for item in code_tools},
        # Code consumes structured results, so this executor does not render them to text.
        ToolExecutor(hooks=hooks, render=False),
        replace(context, code_mode=True),
    )
    stderr = asyncio.create_task(process.stderr.read())
    try:
        async with asyncio.timeout(timeout_seconds):
            outcome = await runner.run(code, f"<run_code-{uuid.uuid4().hex[:8]}>")
    except TimeoutError:
        raise BubError(
            ErrorKind.TOOL,
            f"Code timed out after {timeout_seconds} seconds",
            details={"output": "".join(runner.output)},
        ) from None
    except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
        await _stop(process)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_STOP_TIMEOUT_SECONDS):
                await asyncio.shield(stderr)
        raise BubError(
            ErrorKind.TOOL,
            f"Code runner exited unexpectedly with code {process.returncode}",
            details={
                "output": "".join(runner.output),
                "stderr": stderr.result().decode("utf-8", errors="replace") if stderr.done() else "",
            },
        ) from exc
    finally:
        await _stop(process)
        stderr.cancel()
        await asyncio.gather(stderr, return_exceptions=True)
    output = "".join(runner.output)
    if outcome["type"] == "failed":
        raise BubError(
            ErrorKind.TOOL,
            f"Code raised {outcome.get('error')}",
            details={"output": output, "traceback": str(outcome.get("traceback", ""))},
        )
    return output


@tool(name="code_mode", context=True, agent_use=False)
async def set_code_mode(enable: bool, *, context: ToolContext) -> str:
    """Enable or disable code mode for THIS session. Invoke as the `,code_mode enable=true` command.

    In code mode the model calls preserved tools directly and every other tool from
    Python through `run_code`. Takes effect on the NEXT turn and persists across restarts.
    """
    await set_session_setting(context, CODE_MODE_STATE_KEY, enable)
    return f"Session code mode {'enabled' if enable else 'disabled'} (applies from the next turn)."
