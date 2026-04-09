from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from republic import AsyncStreamEvents, StreamEvent

from bub.builtin.tools import run_subagent
from bub.tools import REGISTRY, tool


class FakeContext:
    """Minimal ToolContext stand-in for testing."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.tape = None


class FakeAgent:
    def __init__(self) -> None:
        self.run = AsyncMock(side_effect=self._run)

    async def _run(self, **kwargs: Any) -> AsyncStreamEvents:
        async def iterator():
            yield StreamEvent("text", {"delta": "agent result"})

        return AsyncStreamEvents(iterator())


@pytest.mark.asyncio
async def test_subagent_inherit_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    result = await run_subagent.run(prompt="do something", session="inherit", context=ctx)

    assert result == "agent result"
    agent.run.assert_called_once()
    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["session_id"] == "user/abc"
    assert call_kwargs["prompt"] == "do something"
    assert call_kwargs["model"] is None


@pytest.mark.asyncio
async def test_subagent_temp_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", session="temp", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["session_id"].startswith("temp/")
    assert call_kwargs["session_id"] != "user/abc"


@pytest.mark.asyncio
async def test_subagent_custom_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", session="custom/session-1", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["session_id"] == "custom/session-1"


@pytest.mark.asyncio
async def test_subagent_passes_model() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", model="openai:gpt-4o", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["model"] == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_subagent_state_includes_session_id() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc", "extra": "val"})

    await run_subagent.run(prompt="task", session="temp", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    state = call_kwargs["state"]
    # State should contain the subagent session_id, not the original
    assert state["session_id"] == call_kwargs["session_id"]
    assert state["extra"] == "val"


@pytest.mark.asyncio
async def test_subagent_default_session_when_missing() -> None:
    """When session_id is not in context state, default to 'temp/unknown'."""
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent})

    await run_subagent.run(prompt="task", session="inherit", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["session_id"] == "temp/unknown"


@pytest.mark.asyncio
async def test_subagent_empty_allowed_tools_defaults_to_all_non_subagent_tools() -> None:
    tool_name = "tests.allowed_tool_default"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name)
    def allowed_tool_default() -> str:
        return "ok"

    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", allowed_tools=[], context=ctx)

    allowed_tools = agent.run.call_args.kwargs["allowed_tools"]
    assert tool_name in allowed_tools
    assert "subagent" not in allowed_tools


@pytest.mark.asyncio
async def test_subagent_resolves_model_tool_aliases_to_runtime_names() -> None:
    tool_name = "tests.resolve_subagent"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name)
    def resolve_subagent() -> str:
        return "ok"

    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", allowed_tools=[" tests_resolve_subagent "], context=ctx)

    assert agent.run.call_args.kwargs["allowed_tools"] == {tool_name}


@pytest.mark.asyncio
async def test_subagent_rejects_unknown_allowed_tools() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    with pytest.raises(ValueError, match="tests_missing_tool"):
        await run_subagent.run(prompt="task", allowed_tools=[" tests_missing_tool "], context=ctx)

    agent.run.assert_not_called()
