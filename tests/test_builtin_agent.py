from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import republic.auth.openai_codex as openai_codex
from republic import TapeContext, ToolAutoResult

import bub.builtin.agent as agent_module
from bub.builtin.agent import Agent
from bub.builtin.settings import AgentSettings


def test_build_llm_passes_codex_resolver_to_republic(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    resolver = object()

    class FakeLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(agent_module, "LLM", FakeLLM)
    monkeypatch.setattr(openai_codex, "openai_codex_oauth_resolver", lambda: resolver)

    settings = AgentSettings(model="openai:gpt-5-codex", api_key=None, api_base=None)
    tape_store = object()

    agent_module._build_llm(settings, tape_store, "ctx")

    assert captured["args"] == ("openai:gpt-5-codex",)
    assert captured["kwargs"]["api_key"] is None
    assert captured["kwargs"]["api_base"] is None
    assert captured["kwargs"]["api_key_resolver"] is resolver
    assert captured["kwargs"]["tape_store"] is tape_store
    assert captured["kwargs"]["context"] == "ctx"


# ---------------------------------------------------------------------------
# Agent.run() tests: merge_back logic and model passthrough
# ---------------------------------------------------------------------------


def _make_agent() -> Agent:
    """Build an Agent with a mocked framework, bypassing real LLM/tape init."""
    framework = MagicMock()
    framework.get_tape_store.return_value = None
    framework.get_system_prompt.return_value = ""

    with patch.object(Agent, "__init__", lambda self, fw: None):
        agent = Agent.__new__(Agent)

    agent.settings = AgentSettings(model="test:model", api_key="k", api_base="b")
    agent.framework = framework
    return agent


class _ForkCapture:
    """Captures the merge_back kwarg passed to fork_tape."""

    def __init__(self) -> None:
        self.merge_back_values: list[bool] = []

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        self.merge_back_values.append(merge_back)
        yield


class _FakeTapeService:
    """Minimal TapeService stand-in for testing Agent.run()."""

    def __init__(self, fork_capture: _ForkCapture) -> None:
        self._fork = fork_capture
        self.run_tools_model: str | None = None

    def session_tape(self, session_id: str, workspace: Any) -> MagicMock:
        tape = MagicMock()
        tape.name = "test-tape"
        tape.context = TapeContext(state={})

        async def fake_run_tools_async(**kwargs: Any) -> ToolAutoResult:
            self.run_tools_model = kwargs.get("model")
            return ToolAutoResult(kind="text", text="done", tool_calls=[], tool_results=[], error=None)

        tape.run_tools_async = fake_run_tools_async
        return tape

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        pass

    async def append_event(self, tape_name: str, name: str, payload: dict) -> None:
        pass

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._fork.fork_tape(tape_name, merge_back=merge_back):
            yield


@pytest.mark.asyncio
async def test_agent_run_regular_session_merges_back() -> None:
    """A regular (non-temp) session should merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    await agent.run(session_id="user/session1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [True]


@pytest.mark.asyncio
async def test_agent_run_temp_session_does_not_merge_back() -> None:
    """A temp/ session should NOT merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    await agent.run(session_id="temp/abc123", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [False]


@pytest.mark.asyncio
async def test_agent_run_passes_model_to_llm() -> None:
    """The model parameter should be forwarded to run_tools_async."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    await agent.run(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"}, model="openai:gpt-4o")  # noqa: S108

    assert fake_tapes.run_tools_model == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_agent_run_empty_prompt_returns_error() -> None:
    agent = _make_agent()
    agent.tapes = MagicMock()  # type: ignore[assignment]

    result = await agent.run(session_id="user/s1", prompt="", state={})

    assert result == "error: empty prompt"


@pytest.mark.asyncio
async def test_agent_run_model_defaults_to_none() -> None:
    """When model is not specified, None should be passed to run_tools_async."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    await agent.run(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fake_tapes.run_tools_model is None
