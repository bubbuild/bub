from __future__ import annotations

from typing import Any

import pytest
from loguru import logger
from pydantic import BaseModel

from bub.tools import REGISTRY, tool, tool_call_reporter


class EchoInput(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_tool_decorator_registers_tool_and_preserves_metadata() -> None:
    tool_name = "tests.sync_tool"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="Sync test tool", model=EchoInput)
    def sync_tool(payload: EchoInput) -> str:
        return payload.value.upper()

    assert sync_tool.name == tool_name
    assert sync_tool.description == "Sync test tool"
    assert REGISTRY[tool_name] is sync_tool
    assert await sync_tool.run(value="hello") == "HELLO"


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
