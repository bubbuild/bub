from __future__ import annotations

from typing import Any

import pytest
from loguru import logger
from pydantic import BaseModel

from bub.tools import EFFECT_KINDS, REGISTRY, enable_effect_log, model_tools, render_tools_prompt, tool


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


def test_model_tools_rewrites_dotted_names_without_mutating_original() -> None:
    tool_name = "tests.rename_me"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="rename")
    def rename_me() -> str:
        return "ok"

    rewritten = model_tools([rename_me])

    assert [item.name for item in rewritten] == ["tests_rename_me"]
    assert rename_me.name == tool_name


def test_render_tools_prompt_renders_available_tools_block() -> None:
    first_name = "tests.prompt_one"
    second_name = "tests.prompt_two"
    REGISTRY.pop(first_name, None)
    REGISTRY.pop(second_name, None)

    @tool(name=first_name, description="First tool")
    def prompt_one() -> str:
        return "one"

    @tool(name=second_name)
    def prompt_two() -> str:
        return "two"

    rendered = render_tools_prompt([prompt_one, prompt_two])

    assert rendered == "<available_tools>\n- tests_prompt_one: First tool\n- tests_prompt_two\n</available_tools>"


def test_render_tools_prompt_returns_empty_string_for_empty_input() -> None:
    assert render_tools_prompt([]) == ""


def test_effect_parameter_populates_effect_kinds_registry() -> None:
    tool_name = "tests.effect_tool"
    REGISTRY.pop(tool_name, None)
    EFFECT_KINDS.pop(tool_name, None)

    @tool(name=tool_name, effect="IrreversibleWrite")
    def effect_tool() -> str:
        return "done"

    assert EFFECT_KINDS[tool_name] == "IrreversibleWrite"
    assert REGISTRY[tool_name] is effect_tool


def test_effect_parameter_none_does_not_register_effect_kind() -> None:
    tool_name = "tests.no_effect_tool"
    REGISTRY.pop(tool_name, None)
    EFFECT_KINDS.pop(tool_name, None)

    @tool(name=tool_name)
    def no_effect_tool() -> str:
        return "done"

    assert tool_name not in EFFECT_KINDS
    assert REGISTRY[tool_name] is no_effect_tool


def test_effect_parameter_works_with_decorator_factory() -> None:
    tool_name = "tests.factory_effect"
    REGISTRY.pop(tool_name, None)
    EFFECT_KINDS.pop(tool_name, None)

    @tool(name=tool_name, description="test", effect="ReadOnly")
    def factory_effect(value: str) -> str:
        return value

    assert EFFECT_KINDS[tool_name] == "ReadOnly"


@pytest.mark.asyncio
async def test_enable_effect_log_wraps_handlers() -> None:
    tool_name = "tests.wrappable"
    REGISTRY.pop(tool_name, None)
    EFFECT_KINDS.pop(tool_name, None)
    call_count = 0

    @tool(name=tool_name, effect="IdempotentWrite")
    def wrappable(msg: str) -> str:
        nonlocal call_count
        call_count += 1
        return f"original: {msg}"

    # Create a mock EffectLog that records calls
    executed: list[tuple[str, dict]] = []

    class MockEffectLog:
        def execute(self, name: str, args: dict):
            executed.append((name, args))
            return f"sealed: {args.get('msg', '')}"

    mock_log = MockEffectLog()
    test_registry = {tool_name: REGISTRY[tool_name]}
    enable_effect_log(mock_log, registry=test_registry)

    # Call the wrapped tool
    result = await test_registry[tool_name].run(msg="hello")

    assert result == "sealed: hello"
    assert len(executed) == 1
    assert executed[0] == (tool_name, {"msg": "hello"})
    assert call_count == 0  # original handler was NOT called


@pytest.mark.asyncio
async def test_enable_effect_log_skips_tools_without_effect() -> None:
    tool_name = "tests.not_wrapped"
    REGISTRY.pop(tool_name, None)
    EFFECT_KINDS.pop(tool_name, None)

    @tool(name=tool_name)
    def not_wrapped(msg: str) -> str:
        return f"original: {msg}"

    class MockEffectLog:
        def execute(self, name: str, args: dict):
            raise AssertionError("should not be called")

    test_registry = {tool_name: REGISTRY[tool_name]}
    enable_effect_log(MockEffectLog(), registry=test_registry)

    # Tool without effect should still run its original handler
    result = await test_registry[tool_name].run(msg="hello")
    assert result == "original: hello"
