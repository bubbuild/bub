from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from loguru import logger
from pydantic import BaseModel

from bub.core.events import EventBus, ToolCallFinished, ToolCallStarted
from bub.core.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.core.tape import Tape, TapeContext
from bub.core.tools import Tool, ToolContext, ToolExecutor, collect_tools, model_tools, tool
from bub.errors import BubError


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
async def test_tool_decorator_preserves_metadata() -> None:
    tool_name = "tests.sync_tool"

    @tool(name=tool_name, description="Sync test tool", model=EchoInput)
    def sync_tool(payload: EchoInput) -> str:
        return payload.value.upper()

    assert sync_tool.name == tool_name
    assert sync_tool.description == "Sync test tool"
    assert sync_tool.exposure == "auto"
    assert await sync_tool.run(value="hello") == "HELLO"


@pytest.mark.asyncio
async def test_tool_decorator_command_exposure_keeps_direct_calls() -> None:
    tool_name = "tests.internal_tool"

    @tool(name=tool_name, exposure="command")
    def internal_tool(value: str) -> str:
        return value.upper()

    assert internal_tool.exposure == "command"
    assert await internal_tool.run("hello") == "HELLO"


@pytest.mark.asyncio
async def test_tool_executor_logs_calls_without_context(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    messages: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        messages.append(message.format(*args, **kwargs))

    monkeypatch.setattr(logger, "info", record)
    echo = Tool.from_callable(lambda value, context: value, name="tests.logged_tool", context=True)

    execution = await ToolExecutor().execute_many([(echo, {"value": "hello"})], context=_context(tmp_path))

    assert execution.tool_results == ["hello"]
    assert len(messages) == 2
    assert messages[0] == 'tool.call.start name=tests.logged_tool { value="hello" }'
    assert messages[1].startswith("tool.call.success name=tests.logged_tool elapsed_time=")


@pytest.mark.asyncio
async def test_tool_executor_logs_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    errors: list[tuple[str, BaseException | None]] = []

    class Recorder:
        def __init__(self, exception: BaseException | None = None) -> None:
            self.exception = exception

        def opt(self, *, exception: BaseException | None = None) -> Recorder:
            return Recorder(exception)

        def info(self, message: str, *args: Any) -> None:
            pass

        def error(self, message: str, *args: Any) -> None:
            errors.append((message.format(*args), self.exception))

    monkeypatch.setattr("bub.core.tools.logger", Recorder())

    def failing_tool() -> str:
        raise RuntimeError("boom")

    execution = await ToolExecutor().execute_many([(Tool.from_callable(failing_tool), {})])

    assert execution.error is not None
    ((message, exception),) = errors
    assert message.startswith("tool.call.error name=failing_tool elapsed_time=")
    assert exception is execution.error
    assert isinstance(exception.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_tool_executor_logs_arguments_changed_by_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(logger, "info", lambda message, *args: messages.append(message.format(*args)))
    events = EventBus()

    @events.on(ToolCallStarted)
    def rewrite(event: ToolCallStarted) -> None:
        event.call = replace(event.call, arguments={"value": "rewritten"})

    echo = Tool.from_callable(lambda value: value, name="tests.echo")
    await ToolExecutor(events).execute_many([(echo, {"value": "original"})])

    assert messages[0] == 'tool.call.start name=tests.echo { value="rewritten" }'


@pytest.mark.asyncio
async def test_tool_decorator_registers_tools_only_while_collecting() -> None:
    def direct_call(value: str) -> str:
        return value.upper()

    outside = tool(direct_call, name="tests.outside")
    with collect_tools() as collected:
        direct_tool = tool(direct_call, name="tests.direct_call")
        with collect_tools() as nested:
            nested_tool = tool(direct_call, name="tests.nested")
        after_nested = tool(direct_call, name="tests.after_nested")

    assert collected == {"tests.direct_call": direct_tool, "tests.after_nested": after_nested}
    assert nested == {"tests.nested": nested_tool}
    assert tool(direct_call, name="tests.later") is not outside
    assert await direct_tool.run("hello") == "HELLO"


def _context(tmp_path, *, code_mode: bool = False) -> ToolContext:
    tape = Tape(AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
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

    @tool(name=tool_name, renderer=lambda result: f"count={result['count']}")
    def counted() -> dict[str, int]:
        return {"count": 3}

    assert counted.render({"count": 3}) == "count=3"
    assert model_tools([counted])[0].render({"count": 1}) == "count=1"


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

    execution = await ToolExecutor(render=render).execute_many(
        [(structured, {})], context=_context(tmp_path, code_mode=code_mode)
    )

    assert execution.error is None
    assert execution.tool_results == [expected]


@pytest.mark.asyncio
async def test_executor_passes_tool_context_to_context_tools_and_hooks(tmp_path) -> None:
    seen: dict[str, Any] = {}

    events = EventBus()
    events.on(ToolCallStarted, lambda event: seen.update(before=event.call.context))
    events.on(ToolCallFinished, lambda event: seen.update(after=(event.call.context, event.result.result)))

    def mode(*, context: ToolContext) -> dict[str, bool]:
        return {"code_mode": context.code_mode}

    context = _context(tmp_path, code_mode=True)
    execution = await ToolExecutor(events, render=False).execute_many(
        [(Tool.from_callable(mode, context=True), {})], context=context
    )

    assert execution.tool_results == [{"code_mode": True}]
    assert seen["before"] is context
    assert seen["after"] == (context, {"code_mode": True})


@pytest.mark.asyncio
async def test_executor_execute_returns_the_result_or_raises_the_tool_error() -> None:
    events = EventBus()
    finished: list[ToolCallFinished] = []
    events.on(ToolCallFinished, finished.append)
    executor = ToolExecutor(events, render=False)

    def failing_tool() -> str:
        raise RuntimeError("boom")

    assert await executor.execute(Tool.from_callable(lambda value: {"value": value}, name="echo"), {"value": 1}) == {
        "value": 1
    }
    with pytest.raises(BubError, match="Tool 'failing_tool' execution failed") as raised:
        await executor.execute(Tool.from_callable(failing_tool), {})

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert [event.call.tool for event in finished] == ["echo", "failing_tool"]


@pytest.mark.asyncio
async def test_executor_reports_renderer_failures_as_tool_errors(tmp_path) -> None:
    def broken_renderer(_result: object) -> str:
        raise KeyError("missing")

    broken = Tool(name="tests.broken_renderer", handler=lambda: {}, renderer=broken_renderer)

    execution = await ToolExecutor().execute_many([(broken, {})], context=_context(tmp_path))

    assert execution.error is not None
    assert execution.tool_results[0].message == "Tool 'tests.broken_renderer' execution failed."
