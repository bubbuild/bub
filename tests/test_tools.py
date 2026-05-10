from __future__ import annotations

from typing import Any

import pytest
from loguru import logger
from pydantic import BaseModel

from bub.tools import REGISTRY, model_tools, render_tools_prompt, resolve_tool_names, tool


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
async def test_tool_direct_call_registers_wrapped_instance_in_registry() -> None:
    tool_name = "tests.direct_call"
    REGISTRY.pop(tool_name, None)

    def direct_call(value: str) -> str:
        return value.upper()

    direct_tool = tool(direct_call, name=tool_name)

    assert REGISTRY[tool_name] is direct_tool
    assert await REGISTRY[tool_name].run("hello") == "HELLO"


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


def test_resolve_tool_names_accepts_runtime_names_and_model_aliases() -> None:
    dotted_name = "tests.resolve_alias"
    underscored_name = "tests_with_underscore"
    REGISTRY.pop(dotted_name, None)
    REGISTRY.pop(underscored_name, None)

    @tool(name=dotted_name)
    def resolve_alias() -> str:
        return "alias"

    @tool(name=underscored_name)
    def resolve_runtime_name() -> str:
        return "runtime"

    assert resolve_tool_names([" tests_resolve_alias ", " tests_with_underscore "], exclude={" subagent "}) == {
        dotted_name,
        underscored_name,
    }
    assert dotted_name not in resolve_tool_names(None, exclude={" tests_resolve_alias "})
    assert resolve_tool_names(None, exclude={" tests_resolve_alias "}) >= {underscored_name}


def test_resolve_tool_names_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="tests_missing_tool"):
        resolve_tool_names([" tests_missing_tool "])

    with pytest.raises(ValueError, match="tests_missing_tool"):
        resolve_tool_names(None, exclude={" tests_missing_tool "})
