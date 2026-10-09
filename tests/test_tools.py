from __future__ import annotations

from typing import Any

import pytest
from loguru import logger
from pydantic import BaseModel

from bub.hooks.interception import ToolCall, ToolCallDecision, ToolCallResult
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.tape import Tape, TapeContext
from bub.tools import REGISTRY, Tool, ToolContext, ToolExecutor, model_tools, tool, tool_call_reporter


class EchoInput(BaseModel):
    value: str


def test_tool_builds_completion_payload() -> None:
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    sample_tool = Tool(
        name="tests_sample_tool",
        description="Sample tool",
        parameters=parameters,
        handler=lambda value: value,
    )

    assert sample_tool.to_schema() == {
        "name": "tests_sample_tool",
        "description": "Sample tool",
        "parameters": parameters,
    }


def test_model_tools_rewrites_dotted_names_without_mutating_original() -> None:
    tool_name = "tests.rename_me"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="rename")
    def rename_me(value: str) -> str:
        return "ok"

    rewritten = model_tools([rename_me])

    assert [item.name for item in rewritten] == ["tests_rename_me"]
    assert rewritten[0].parameters == rename_me.parameters
    assert rename_me.name == tool_name
    assert "additionalProperties" not in rename_me.parameters


def test_model_tools_excludes_command_tools() -> None:
    visible_tool = Tool(name="tests.visible", handler=lambda: None)
    code_tool = Tool(name="tests.code", handler=lambda: None, exposure="code")
    internal_tool = Tool(name="tests.internal", handler=lambda: None, exposure="command")

    rewritten = model_tools([visible_tool, code_tool, internal_tool])

    assert [item.name for item in rewritten] == ["tests_visible", "tests_code"]


@pytest.mark.asyncio
async def test_tool_decorator_registers_tool_and_preserves_metadata() -> None:
    tool_name = "tests.sync_tool"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="Sync test tool", model=EchoInput)
    def sync_tool(payload: EchoInput) -> str:
        return payload.value.upper()

    assert sync_tool.name == tool_name
    assert sync_tool.description == "Sync test tool"
    assert sync_tool.exposure == "auto"
    assert REGISTRY[tool_name] is sync_tool
    assert await sync_tool.run(value="hello") == "HELLO"


@pytest.mark.asyncio
async def test_tool_decorator_command_exposure_keeps_direct_calls() -> None:
    tool_name = "tests.internal_tool"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, exposure="command")
    def internal_tool(value: str) -> str:
        return value.upper()

    assert internal_tool.exposure == "command"
    assert REGISTRY[tool_name] is internal_tool
    assert await internal_tool.run("hello") == "HELLO"


@pytest.mark.asyncio
async def test_tool_wrapper_logs_and_omits_context_from_log_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_name = "tests.async_tool"
    REGISTRY.pop(tool_name, None)
    messages: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        messages.append(message.format(*args, **kwargs))

    monkeypatch.setattr(logger, "info", record)

    @tool(name=tool_name, description="Async test tool", context=True)
    async def async_tool(value: str, context: object) -> str:
        return f"{value}:{context}"

    result = await async_tool.run("hello", context="ctx")

    assert result == "hello:ctx"
    assert REGISTRY[tool_name] is async_tool
    assert len(messages) == 2
    assert messages[0] == 'tool.call.start name=tests.async_tool { "hello" }'
    assert messages[1].startswith("tool.call.success name=tests.async_tool elapsed_time=")


@pytest.mark.asyncio
async def test_tool_wrapper_logs_failures_before_reraising(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_name = "tests.failing_tool"
    REGISTRY.pop(tool_name, None)
    errors: list[str] = []

    def record_exception(message: str, *args: Any, **kwargs: Any) -> None:
        errors.append(message.format(*args, **kwargs))

    monkeypatch.setattr(logger, "exception", record_exception)

    @tool(name=tool_name)
    def failing_tool() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await failing_tool.run()

    assert len(errors) == 1
    assert errors[0].startswith("tool.call.error name=tests.failing_tool elapsed_time=")


@pytest.mark.asyncio
async def test_tool_wrapper_uses_reporter_instead_of_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_name = "tests.reported_tool"
    REGISTRY.pop(tool_name, None)
    logged: list[str] = []
    reported: list[tuple[str, str, Any]] = []

    def record_log(message: str, *args: Any, **kwargs: Any) -> None:
        logged.append(message.format(*args, **kwargs))

    class Reporter:
        def start(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            reported.append(("start", name, {"args": args, "kwargs": kwargs}))

        def success(self, name: str, result: Any, elapsed_ms: float) -> None:
            reported.append(("success", name, {"result": result, "elapsed_ms": elapsed_ms}))

        def error(self, name: str, error: BaseException, elapsed_ms: float) -> None:
            reported.append(("error", name, {"error": error, "elapsed_ms": elapsed_ms}))

    monkeypatch.setattr(logger, "info", record_log)
    monkeypatch.setattr(logger, "exception", record_log)

    @tool(name=tool_name)
    def reported_tool(value: str) -> str:
        return value.upper()

    with tool_call_reporter(Reporter()):
        result = await reported_tool.run("hello")

    assert result == "HELLO"
    assert logged == []
    assert reported[0] == ("start", tool_name, {"args": ("hello",), "kwargs": {}})
    assert reported[1][0] == "success"
    assert reported[1][1] == tool_name
    assert reported[1][2]["result"] == "HELLO"


@pytest.mark.asyncio
async def test_tool_direct_call_registers_wrapped_instance_in_registry() -> None:
    tool_name = "tests.direct_call"
    REGISTRY.pop(tool_name, None)

    def direct_call(value: str) -> str:
        return value.upper()

    direct_tool = tool(direct_call, name=tool_name)

    assert REGISTRY[tool_name] is direct_tool
    assert await REGISTRY[tool_name].run("hello") == "HELLO"


def _context(tmp_path, *, code_mode: bool = False) -> ToolContext:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    return ToolContext(tape=tape, run_id="run-1", code_mode=code_mode)


def test_tool_render_defaults_to_json_for_structured_results() -> None:
    sample = Tool(name="tests.render_default", handler=lambda: None)

    assert sample.render("plain") == "plain"
    assert sample.render({"value": "é"}) == '{"value": "é"}'
    assert sample.render(EchoInput(value="x")) == '{"value":"x"}'


def test_tool_rejects_unknown_exposure() -> None:
    with pytest.raises(ValueError, match="unknown exposure 'model'"):
        Tool(name="tests.exposure", handler=lambda: None, exposure="model")  # type: ignore[arg-type]


def test_tool_decorator_accepts_renderer() -> None:
    tool_name = "tests.custom_renderer"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, renderer=lambda result: f"count={result['count']}")
    def counted() -> dict[str, int]:
        return {"count": 3}

    try:
        assert counted.render({"count": 3}) == "count=3"
        assert model_tools([counted])[0].render({"count": 1}) == "count=1"
    finally:
        REGISTRY.pop(tool_name, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("render", "code_mode", "expected"),
    [(True, False, "count=3"), (True, True, "count=3"), (False, False, {"count": 3}), (False, True, {"count": 3})],
)
async def test_executor_render_option_decides_result_format(
    tmp_path, render: bool, code_mode: bool, expected: object
) -> None:
    structured = Tool(
        name="tests.structured",
        handler=lambda: {"count": 3},
        renderer=lambda result: f"count={result['count']}",
    )

    execution = await ToolExecutor(render=render).execute_async(
        [(structured, {})], context=_context(tmp_path, code_mode=code_mode)
    )

    assert execution.error is None
    assert execution.tool_results == [expected]


@pytest.mark.asyncio
async def test_executor_passes_tool_context_to_context_tools_and_hooks(tmp_path) -> None:
    seen: dict[str, Any] = {}

    class RecordingHooks:
        async def before_tool_call(self, call: ToolCall, state: dict[str, Any]) -> tuple[ToolCall, ToolCallDecision]:
            seen["before"] = call.context
            return call, ToolCallDecision.proceed()

        async def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict[str, Any]) -> None:
            seen["after"] = (call.context, result.result)

    def mode(*, context: ToolContext) -> dict[str, bool]:
        return {"code_mode": context.code_mode}

    context = _context(tmp_path, code_mode=True)
    execution = await ToolExecutor(hooks=RecordingHooks(), render=False).execute_async(  # type: ignore[arg-type]
        [(Tool.from_callable(mode, context=True), {})], context=context
    )

    assert execution.tool_results == [{"code_mode": True}]
    assert seen["before"] is context
    assert seen["after"] == (context, {"code_mode": True})


@pytest.mark.asyncio
async def test_executor_reports_renderer_failures_as_tool_errors(tmp_path) -> None:
    def broken_renderer(_result: object) -> str:
        raise KeyError("missing")

    broken = Tool(name="tests.broken_renderer", handler=lambda: {}, renderer=broken_renderer)

    execution = await ToolExecutor().execute_async([(broken, {})], context=_context(tmp_path))

    assert execution.error is not None
    assert execution.tool_results[0]["message"] == "Tool 'tests.broken_renderer' execution failed."
