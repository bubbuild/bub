from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from any_llm.types.completion import ChatCompletionChunk

from bub.builtin.agent import Agent
from bub.builtin.settings import AgentSettings
from bub.runtime import BubError
from bub.tape import Tape, TapeContext
from bub.tools import REGISTRY, tool

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

    agent.settings = AgentSettings.model_construct(model="test:model", api_key="k", api_base="b", client_args={})
    agent.framework = framework

    async def fake_completion_response(**kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
        agent.completion_kwargs = kwargs
        return _chat_stream("done")

    agent.completion_kwargs = None
    agent._completion_response = fake_completion_response  # type: ignore[method-assign]
    return agent


def _chat_chunk(content: str) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate({
        "id": "chatcmpl_test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test:model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "delta": {"role": "assistant", "content": content},
            }
        ],
    })


async def _chat_stream(content: str) -> AsyncIterator[ChatCompletionChunk]:
    yield _chat_chunk(content)


class _ForkCapture:
    """Captures fork_tape enter and exit behavior."""

    def __init__(self) -> None:
        self.merge_back_values: list[bool] = []
        self.exit_count = 0

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        self.merge_back_values.append(merge_back)
        try:
            yield
        finally:
            self.exit_count += 1


class _FakeTapeService:
    """Minimal TapeService stand-in for testing Agent.run()."""

    def __init__(self, fork_capture: _ForkCapture) -> None:
        self._fork = fork_capture
        self.messages: list[dict[str, Any]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def session_tape(self, session_id: str, workspace: Any) -> Tape:
        return Tape(name="test-tape", context=TapeContext(state={}))

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        pass

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._fork.fork_tape(tape_name, merge_back=merge_back):
            yield

    async def read_messages(self, tape: Tape) -> list[dict[str, Any]]:
        return list(self.messages)

    async def append_event(self, tape_name: str, name: str, payload: dict[str, Any], **meta: Any) -> None:
        self.events.append((tape_name, name, payload))

    async def record_chat(
        self,
        *,
        tape: str,
        run_id: str,
        system_prompt: str | None,
        new_messages: list[dict[str, Any]],
        response_text: str | None,
        context_error: BubError | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_results: list[Any] | None = None,
        error: BubError | None = None,
        response: Any | None = None,
        provider: str | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        if system_prompt:
            self.events.append((tape, "system", {"content": system_prompt}))
        if context_error is not None:
            self.events.append((tape, "error", context_error.as_dict()))
        self.messages.extend(new_messages)
        if tool_calls:
            self.events.append((tape, "tool_call", {"calls": tool_calls}))
        if tool_results is not None:
            self.events.append((tape, "tool_result", {"results": tool_results}))
        if error is not None and error is not context_error:
            self.events.append((tape, "error", error.as_dict()))
        if response_text is not None:
            self.messages.append({"role": "assistant", "content": response_text})
        self.events.append((tape, "run", {"run_id": run_id, "model": model, "error": error is not None}))


@pytest.mark.asyncio
async def test_agent_run_regular_session_merges_back() -> None:
    """A regular (non-temp) session should merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    result = await agent.run_stream(session_id="user/session1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [True]
    assert fork_capture.exit_count == 0

    [event async for event in result]

    assert fork_capture.merge_back_values == [True]
    assert fork_capture.exit_count == 1


@pytest.mark.asyncio
async def test_agent_run_temp_session_does_not_merge_back() -> None:
    """A temp/ session should NOT merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    result = await agent.run_stream(session_id="temp/abc123", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [False]
    assert fork_capture.exit_count == 0

    [event async for event in result]

    assert fork_capture.merge_back_values == [False]
    assert fork_capture.exit_count == 1


@pytest.mark.asyncio
async def test_agent_run_passes_model_to_llm() -> None:
    """The model parameter should be forwarded to any-llm."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        model="openai:gpt-4o",
    )
    [event async for event in result]

    assert agent.completion_kwargs["model"] == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_agent_run_empty_prompt_returns_error() -> None:
    agent = _make_agent()
    agent.tapes = MagicMock()  # type: ignore[assignment]

    result = await agent.run_stream(session_id="user/s1", prompt="", state={})
    events = [event async for event in result]

    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": "error: empty prompt"}),
        ("final", {"ok": False, "text": "error: empty prompt"}),
    ]


@pytest.mark.asyncio
async def test_agent_run_model_defaults_to_none() -> None:
    """When model is not specified, settings.model is used for any-llm."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run_stream(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    assert agent.completion_kwargs["model"] == "test:model"


@pytest.mark.asyncio
async def test_agent_run_resolves_allowed_tool_aliases_and_limits_prompt() -> None:
    allowed_name = "tests.allowed_agent_tool"
    denied_name = "tests.denied_agent_tool"
    REGISTRY.pop(allowed_name, None)
    REGISTRY.pop(denied_name, None)

    @tool(name=allowed_name, description="Allowed tool")
    def allowed_agent_tool() -> str:
        return "allowed"

    @tool(name=denied_name, description="Denied tool")
    def denied_agent_tool() -> str:
        return "denied"

    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        allowed_tools=[" tests_allowed_agent_tool "],
    )
    [event async for event in result]

    assert agent.completion_kwargs is not None
    assert [tool.name for tool in agent.completion_kwargs["tools"]] == ["tests_allowed_agent_tool"]
    system_prompt = agent.completion_kwargs["messages"][0]["content"]
    assert "- tests_allowed_agent_tool(): Allowed tool" in system_prompt
    assert "tests_denied_agent_tool" not in system_prompt


@pytest.mark.asyncio
async def test_agent_run_rejects_unknown_allowed_tools() -> None:
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    stream = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        allowed_tools=["tests_missing_agent_tool"],
    )

    with pytest.raises(ValueError, match="tests_missing_agent_tool"):
        [event async for event in stream]
