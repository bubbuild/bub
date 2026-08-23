from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bub.builtin.tools import run_subagent
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub.tools import REGISTRY, tool


class FakeContext:
    """Minimal ToolContext stand-in for testing."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.tape = state["_runtime_agent"].tape


class FakeTape:
    def __init__(self, forkmerge: object) -> None:
        self.forkmerge = forkmerge

    def get_sidecar(self, name: str) -> object | None:
        return self.forkmerge if name == "forkmerge" else None


class FakeAgent:
    def __init__(self) -> None:
        self.tape_fork = SimpleNamespace(tape=object(), merge=AsyncMock(), discard=AsyncMock())
        self.forkmerge = SimpleNamespace(fork=AsyncMock(return_value=self.tape_fork))
        self.tape = FakeTape(self.forkmerge)
        self.session_tape = MagicMock(side_effect=self._session_tape)
        self.run_stream = AsyncMock(side_effect=self._run_stream)

    def _session_tape(self, session_id: str, state: dict[str, Any], *, source=None) -> FakeTape:
        return source or self.tape

    async def _run_stream(self, **kwargs: Any) -> AsyncStreamEvents:
        async def iterator():
            yield StreamEvent("text", {"delta": "agent result"})

        return AsyncStreamEvents(iterator())


@pytest.mark.asyncio
async def test_subagent_inherit_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    result = await run_subagent.run(prompt="do something", session="inherit", context=ctx)

    assert result == "agent result"
    agent.run_stream.assert_called_once()
    tape_args = agent.session_tape.call_args.args
    assert tape_args[0] == "user/abc"
    call_kwargs = agent.run_stream.call_args.kwargs
    assert call_kwargs["prompt"] == "do something"
    assert call_kwargs["model"] is None
    agent.tape_fork.merge.assert_awaited_once()


@pytest.mark.asyncio
async def test_subagent_temp_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", session="temp", context=ctx)

    subagent_session = agent.session_tape.call_args.args[0]
    assert subagent_session.startswith("temp/")
    assert subagent_session != "user/abc"
    agent.tape_fork.discard.assert_awaited_once()


@pytest.mark.asyncio
async def test_subagent_custom_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", session="custom/session-1", context=ctx)

    assert agent.session_tape.call_args.args[0] == "custom/session-1"


@pytest.mark.asyncio
async def test_subagent_passes_model() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", model="openai:gpt-4o", context=ctx)

    call_kwargs = agent.run_stream.call_args.kwargs
    assert call_kwargs["model"] == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_subagent_state_includes_session_id() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc", "extra": "val"})

    await run_subagent.run(prompt="task", session="temp", context=ctx)

    session_id, state = agent.session_tape.call_args.args
    # Turn state should contain the subagent session_id, not the original
    assert state["session_id"] == session_id
    assert state["extra"] == "val"


@pytest.mark.asyncio
async def test_subagent_default_session_when_missing() -> None:
    """When session_id is not in context state, default to 'temp/unknown'."""
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent})

    await run_subagent.run(prompt="task", session="inherit", context=ctx)

    assert agent.session_tape.call_args.args[0] == "temp/unknown"


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

    allowed_tools = agent.run_stream.call_args.kwargs["allowed_tools"]
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

    assert agent.run_stream.call_args.kwargs["allowed_tools"] == {tool_name}


@pytest.mark.asyncio
async def test_subagent_rejects_unknown_allowed_tools() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    with pytest.raises(ValueError, match="tests_missing_tool"):
        await run_subagent.run(prompt="task", allowed_tools=[" tests_missing_tool "], context=ctx)

    agent.run_stream.assert_not_called()
