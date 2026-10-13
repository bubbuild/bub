from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import republic

import bub.builtin.codemode
import bub.builtin.tools  # noqa: F401  — registers builtin tools (incl. `model`)
from bub.builtin.agent import BubAgent
from bub.builtin.settings import AgentSettings
from bub.builtin.steering import InMemorySteeringInbox
from bub.core import PromptContext, StepFinished
from bub.core.context import default_tape_context
from bub.core.tape import session_tape_name
from bub.prompt import UserContent
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tape import Tape
from bub.tools import REGISTRY, tool
from tests.model_fakes import ProviderService

# ---------------------------------------------------------------------------
# Agent.run() tests: merge_back logic and model passthrough
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def model_provider(provider_transport: ProviderService) -> ProviderService:
    provider_transport.reply_chat()
    provider_transport.reply_chat()
    return provider_transport


def _make_agent() -> BubAgent:
    """Build an Agent with a mocked framework, bypassing real LLM/tape init."""
    framework = MagicMock()
    framework.get_tape_store.return_value = None
    framework.get_steering_inbox.return_value = None
    framework.get_system_prompt.return_value = ""
    framework.get_tape_sidecars.return_value = ()

    async def build_prompt(message: dict[str, Any], session_id: str, state: dict[str, Any]) -> list[UserContent]:
        return [str(message["content"])]

    framework.build_prompt = build_prompt

    settings = AgentSettings.model_construct(
        model="openai:model", api_key="k", api_base=None, client_args={"api_format": "chat"}
    )
    with patch("bub.builtin.agent.load_settings", return_value=settings):
        agent = BubAgent(framework)
    agent.__dict__["_tapes"] = Tape(AsyncTapeStoreAdapter(InMemoryTapeStore()), default_tape_context())
    return agent


class _ForkCapture:
    """Records how session tapes are forked and when the forks close."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.merge_back_values: list[bool] = []
        self.exit_count = 0
        fork_tape = Tape.fork_tape

        @contextlib.asynccontextmanager
        async def capture(tape: Tape, merge_back: bool = True) -> AsyncGenerator[Tape, None]:
            self.merge_back_values.append(merge_back)
            try:
                async with fork_tape(tape, merge_back=merge_back) as forked:
                    yield forked
            finally:
                self.exit_count += 1

        monkeypatch.setattr(Tape, "fork_tape", capture)


@pytest.fixture
def fork_capture(monkeypatch: pytest.MonkeyPatch) -> _ForkCapture:
    return _ForkCapture(monkeypatch)


@pytest.mark.asyncio
async def test_agent_run_regular_session_merges_back(fork_capture: _ForkCapture) -> None:
    """A regular (non-temp) session should merge tape entries back."""
    agent = _make_agent()

    result = await agent.run_stream(session_id="user/session1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [True]
    assert fork_capture.exit_count == 0

    [event async for event in result]

    assert fork_capture.merge_back_values == [True]
    assert fork_capture.exit_count == 1


@pytest.mark.asyncio
async def test_agent_run_temp_session_does_not_merge_back(fork_capture: _ForkCapture) -> None:
    """A temp/ session should NOT merge tape entries back."""
    agent = _make_agent()

    result = await agent.run_stream(session_id="temp/abc123", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [False]
    assert fork_capture.exit_count == 0

    [event async for event in result]

    assert fork_capture.merge_back_values == [False]
    assert fork_capture.exit_count == 1


@pytest.mark.asyncio
async def test_agent_run_passes_model_to_llm(
    model_provider: ProviderService,
) -> None:
    """The model parameter should be forwarded to Republic."""
    agent = _make_agent()

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        model="openai:gpt-4o",
    )
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    assert completion_kwargs["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_agent_run_empty_prompt_returns_error() -> None:
    agent = _make_agent()

    result = await agent.run_stream(session_id="user/s1", prompt="", state={})
    events = [event async for event in result]

    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": "error: empty prompt"}),
        ("final", {"ok": False, "text": "error: empty prompt"}),
    ]


@pytest.mark.asyncio
async def test_agent_run_model_defaults_to_none(
    model_provider: ProviderService,
) -> None:
    """When model is not specified, settings.model is used for Republic."""
    agent = _make_agent()

    result = await agent.run_stream(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    assert completion_kwargs["model"] == "model"


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", ["custom continuation", None])
async def test_agent_loop_awaits_continue_prompt_hook_with_stream_state(continuation: str | None) -> None:
    agent = _make_agent()
    tape = agent.tape("continuation")
    prompts: list[list[UserContent] | None] = []
    continuation_prompts: list[list[UserContent] | None] = []
    observed_usage: list[dict[str, Any] | None] = []

    def run_once(prompt: list[UserContent] | None, *, model: Any, tape: Tape, **_: Any) -> AsyncStreamEvents:
        prompts.append(prompt)
        should_continue = len(prompts) == 1

        async def iterator() -> AsyncIterator[StreamEvent]:
            yield StreamEvent("final", {"tool_calls": ["call"] if should_continue else []})

        return AsyncStreamEvents(iterator(), state=StreamState(usage={"step": len(prompts)}))

    async def continue_prompt(*, prompt: list[UserContent] | None, tape: Tape, state: StreamState) -> str | None:
        continuation_prompts.append(prompt)
        observed_usage.append(state.usage)
        return continuation

    agent.framework.continue_prompt = continue_prompt
    agent.run_once = run_once  # type: ignore[method-assign]

    events = [event async for event in await agent.run_on(tape, ["initial prompt"], model=agent.settings.chat_model())]

    assert [event.kind for event in events] == ["final", "final"]
    assert prompts == [["initial prompt"], None if continuation is None else [continuation]]
    assert continuation_prompts == [["initial prompt"]]
    assert observed_usage == [{"step": 1}]


@pytest.mark.asyncio
async def test_agent_run_model_override_does_not_mutate_default(
    model_provider: ProviderService,
) -> None:
    """A per-call model override must not leak into the agent's configured model.

    The override is resolved per turn (``model or self.settings.model``) and
    forwarded to Republic; it must never be written back to ``settings.model``.
    This is the agent-layer half of the guarantee that a session-scoped model
    switch (state['model'] -> run_stream(model=...)) cannot bleed across
    sessions the way a process-global env var would.
    """
    agent = _make_agent()
    default_model = agent.settings.model

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        model="openai:gpt-4o",
    )
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    assert completion_kwargs["model"] == "gpt-4o"
    assert agent.settings.model == default_model


@pytest.mark.asyncio
async def test_agent_run_injects_steering_messages_once_by_session(
    model_provider: ProviderService,
) -> None:
    agent = _make_agent()
    steering_inbox = InMemorySteeringInbox()
    agent.framework.get_steering_inbox.return_value = steering_inbox

    await steering_inbox.enqueue_message(
        {"session_id": "other-session", "content": "ignore me"}, {"session_id": "other-session"}
    )
    await steering_inbox.enqueue_message({"session_id": "user/s1", "content": "first steer"}, {"session_id": "user/s1"})
    await steering_inbox.enqueue_message(
        {"session_id": "user/s1", "content": "second steer"}, {"session_id": "user/s1"}
    )

    result = await agent.run_stream(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    completion_messages = completion_kwargs["messages"]
    assert completion_messages[-3:] == [
        {"role": "user", "content": "first steer"},
        {"role": "user", "content": "second steer"},
        {"role": "user", "content": "hello"},
    ]
    messages = await agent.tape(session_tape_name("user/s1", Path("/tmp"))).read_messages()  # noqa: S108
    assert messages[-4:] == [
        republic.user("first steer"),
        republic.user("second steer"),
        republic.user("hello"),
        republic.assistant("done"),
    ]

    result = await agent.run_stream(session_id="user/s1", prompt="again", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    completion_messages = completion_kwargs["messages"]
    assert completion_messages[-1] == {"role": "user", "content": "again"}
    assert {"role": "user", "content": "ignore me"} not in completion_messages


@pytest.mark.asyncio
async def test_agent_run_resolves_allowed_tool_aliases_and_limits_prompt(
    model_provider: ProviderService,
) -> None:
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

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        allowed_tools=[" tests_allowed_agent_tool "],
    )
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    assert [tool["function"]["name"] for tool in completion_kwargs["tools"]] == ["tests_allowed_agent_tool"]
    system_prompt = completion_kwargs["messages"][0]["content"]
    assert "tests_allowed_agent_tool" not in system_prompt
    assert "tests_denied_agent_tool" not in system_prompt


@pytest.mark.asyncio
async def test_agent_run_excludes_command_tools(
    model_provider: ProviderService,
) -> None:
    visible_name = "tests.visible_agent_tool"
    internal_name = "tests.internal_agent_tool"
    REGISTRY.pop(visible_name, None)
    REGISTRY.pop(internal_name, None)

    @tool(name=visible_name, description="Visible tool")
    def visible_agent_tool() -> str:
        return "visible"

    @tool(name=internal_name, description="Internal tool", exposure="command")
    def internal_agent_tool() -> str:
        return "internal"

    agent = _make_agent()

    result = await agent.run_stream(
        session_id="user/s1",
        prompt="hello",
        state={"_runtime_workspace": "/tmp"},  # noqa: S108
        allowed_tools=[visible_name, internal_name],
    )
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    assert [tool["function"]["name"] for tool in completion_kwargs["tools"]] == ["tests_visible_agent_tool"]
    system_prompt = completion_kwargs["messages"][0]["content"]
    assert "tests_visible_agent_tool" not in system_prompt
    assert "tests_internal_agent_tool" not in system_prompt


@pytest.mark.asyncio
async def test_agent_run_rejects_unknown_allowed_tools(fork_capture: _ForkCapture) -> None:
    agent = _make_agent()

    with pytest.raises(ValueError, match="tests_missing_agent_tool"):
        await agent.run_stream(
            session_id="user/s1",
            prompt="hello",
            state={"_runtime_workspace": "/tmp"},  # noqa: S108
            allowed_tools=["tests_missing_agent_tool"],
        )
    assert fork_capture.merge_back_values == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [",bash command='echo hello'", ",echo hello"])
async def test_run_command_executes_shell_with_command_parameter(prompt: str, tmp_path) -> None:
    agent = _make_agent()

    stream = await agent.run_stream(session_id="user/s1", prompt=prompt, state={"_runtime_workspace": str(tmp_path)})
    events = [event async for event in stream]

    assert not any(event.kind == "error" for event in events)
    assert any(event.data.get("delta") == "hello" for event in events if event.kind == "text")


@pytest.mark.asyncio
async def test_run_command_model_switches_session_model_directly() -> None:
    """,model <model_id> runs the `model` builtin as a chat command with no LLM call.

    The command writes state['model'] on the same state object the framework
    hands to run_stream, so the override is picked up by run_model_stream on the
    next turn. Persistence is a `model_switch` event the tool records on the
    session tape (merged back at end of turn), not a side effect of save_state.
    """
    agent = _make_agent()
    state: dict[str, Any] = {"_runtime_workspace": "/tmp"}  # noqa: S108
    assert "model" not in REGISTRY or REGISTRY["model"].context is True

    stream = await agent.run_stream(session_id="user/s1", prompt=",model openai:gpt-4o", state=state)
    events = [event async for event in stream]

    # state["model"] is mutated in place (tool context shares the framework state).
    assert state["model"] == "openai:gpt-4o"
    deltas = [event.data.get("delta", "") for event in events if event.kind == "text"]
    assert any("Session model set to openai:gpt-4o" in delta for delta in deltas)


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_run_code", [True, False])
async def test_code_mode_exposes_preserved_tools_and_run_code_with_stub(
    model_provider: ProviderService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow_run_code: bool
) -> None:
    from bub.builtin.codemode import CODE_TOOLS_STATE_KEY, RUN_CODE_TOOL_NAME, run_code

    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    agent = _make_agent()
    agent.tools[RUN_CODE_TOOL_NAME] = run_code
    allowed_tools = ["bash", "bash.output", "fs.read", "tape.info", *([RUN_CODE_TOOL_NAME] if allow_run_code else [])]
    state: dict[str, Any] = {"_runtime_workspace": str(tmp_path), "code_mode": True}

    result = await agent.run_stream(session_id="user/s1", prompt="hello", state=state, allowed_tools=allowed_tools)
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    tool_names = [tool["function"]["name"] for tool in completion_kwargs["tools"]]
    system_prompt = completion_kwargs["messages"][0]["content"]
    if not allow_run_code:
        assert sorted(tool_names) == ["bash", "bash_output", "fs_read", "tape_info"]
        assert "<code_mode>" not in system_prompt
        assert CODE_TOOLS_STATE_KEY not in state
        return

    assert sorted(tool_names) == ["bash", "bash_output", "fs_read", RUN_CODE_TOOL_NAME]
    assert "tape_info" not in system_prompt.split("<code_mode>")[0]
    stub_path = Path(system_prompt.split("stub file: ", 1)[1].splitlines()[0])
    assert stub_path.is_relative_to(tmp_path / "home" / "codemode")
    stub = stub_path.read_text(encoding="utf-8")
    assert "async def tape_info() -> TapeInfoResult:" in stub
    assert "async def bash(" not in stub
    assert "async def bash_output(" not in stub
    assert "async def fs_read(" not in stub
    assert "def run_code(" not in stub
    assert [tool.name for tool in state[CODE_TOOLS_STATE_KEY]] == ["tape_info"]


@pytest.mark.asyncio
async def test_run_code_is_hidden_when_session_code_mode_is_off(
    model_provider: ProviderService, tmp_path: Path
) -> None:
    from bub.builtin.codemode import RUN_CODE_TOOL_NAME, run_code

    agent = _make_agent()
    agent.tools[RUN_CODE_TOOL_NAME] = run_code

    result = await agent.run_stream(session_id="user/s1", prompt="hello", state={"_runtime_workspace": str(tmp_path)})
    [event async for event in result]

    completion_kwargs = model_provider.body()
    assert completion_kwargs is not None
    assert RUN_CODE_TOOL_NAME not in [tool["function"]["name"] for tool in completion_kwargs["tools"]]
    assert "tape_info" in [tool["function"]["name"] for tool in completion_kwargs["tools"]]
    assert "<code_mode>" not in completion_kwargs["messages"][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "expected"), [(",code_mode enable=true", True), (",code_mode false", False)])
async def test_code_mode_command_switches_session_code_mode(command: str, expected: bool) -> None:
    agent = _make_agent()
    state: dict[str, Any] = {"_runtime_workspace": "/tmp"}  # noqa: S108

    stream = await agent.run_stream(session_id="user/s1", prompt=command, state=state)
    events = [event async for event in stream]

    assert state["code_mode"] is expected
    deltas = [event.data.get("delta", "") for event in events if event.kind == "text"]
    status = "enabled" if expected else "disabled"
    assert f"Session code mode {status} (applies from the next turn)." in deltas


@pytest.mark.asyncio
async def test_agent_sections_and_handlers_apply_after_builtin_ones(model_provider: ProviderService) -> None:
    agent = _make_agent()
    agent.skill_dirs = []
    agent.framework.get_system_prompt.return_value = "<framework/>"
    statuses: list[str] = []

    @agent.system_prompt.add
    def session(context: PromptContext) -> str:
        return f"<session>{context.state['session_id']}:{len(context.tools)}</session>"

    @agent.events.on(StepFinished)
    def record(event: StepFinished) -> None:
        statuses.append(event.status)

    result = await agent.run_stream(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108
    [event async for event in result]

    request = model_provider.body()
    system_prompt = request["messages"][0]["content"]
    assert system_prompt.endswith(f"<session>user/s1:{len(request['tools'])}</session>")
    assert system_prompt.startswith("<framework/>\n\n")
    assert statuses == ["ok"]
