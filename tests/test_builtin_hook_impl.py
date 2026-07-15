from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from bub.builtin.hook_impl import AGENTS_FILE_NAME, DEFAULT_SYSTEM_PROMPT, BuiltinImpl
from bub.builtin.store import FileTapeStore
from bub.builtin.tape import Tape
from bub.channels.message import ChannelMessage
from bub.framework import BubFramework
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeContext


class RecordingLifespan:
    def __init__(self) -> None:
        self.entered = False
        self.exit_args: tuple[object, object, object] | None = None

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exit_args = (exc_type, exc, traceback)


def _fake_tape(home: Path) -> Tape:
    return Tape(
        archive_path=home / "tapes",
        store=AsyncTapeStoreAdapter(InMemoryTapeStore()),
        context=TapeContext(),
    )


class FakeAgent:
    def __init__(self, home: Path, *, tape: Tape | None = None) -> None:
        self.settings = SimpleNamespace(home=home)
        # A real in-memory async tape so load_state's recovery path runs against
        # the same store the tests write `model_switch` events to.
        self.tape = tape if tape is not None else _fake_tape(home)
        self.run_calls: list[tuple[str, str, dict[str, object]]] = []
        self.run_stream_calls: list[tuple[str, str, dict[str, object], str | None]] = []

    async def run(self, *, session_id: str, prompt: str, state: dict[str, object]) -> str:
        self.run_calls.append((session_id, prompt, state))
        return "agent-output"

    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str,
        state: dict[str, object],
        model: str | None = None,
    ) -> AsyncStreamEvents:
        self.run_stream_calls.append((session_id, prompt, state, model))

        async def iterator():
            yield StreamEvent("text", {"delta": "agent-output"})

        return AsyncStreamEvents(iterator())


def _raise_value_error() -> None:
    raise ValueError("boom")


def _build_impl(tmp_path: Path, config_file: Path | None = None) -> tuple[BubFramework, BuiltinImpl, FakeAgent]:
    framework = BubFramework(config_file=config_file) if config_file is not None else BubFramework()
    impl = BuiltinImpl(framework)
    agent = FakeAgent(tmp_path)
    impl._agent = agent
    return framework, impl, agent


def test_resolve_session_prefers_explicit_session_id(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    message = ChannelMessage(session_id="  keep-me  ", channel="cli", chat_id="room", content="hello")

    assert impl.resolve_session(message) == "  keep-me  "


def test_resolve_session_falls_back_to_channel_and_chat_id(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    message = {"session_id": "   ", "channel": "telegram", "chat_id": "42", "content": "hello"}

    assert impl.resolve_session(message) == "telegram:42"


@pytest.mark.asyncio
async def test_load_state_and_save_state_manage_lifespan_and_context(tmp_path: Path) -> None:
    _, impl, agent = _build_impl(tmp_path)
    lifespan = RecordingLifespan()
    message = ChannelMessage(
        session_id="session",
        channel="cli",
        chat_id="room",
        content="hello",
        lifespan=lifespan,
    )

    state = await impl.load_state(message=message, session_id="resolved-session")

    assert lifespan.entered is True
    assert state["session_id"] == "resolved-session"
    assert state["_runtime_agent"] is agent
    assert state["context"] == message.context_str

    try:
        _raise_value_error()
    except ValueError as exc:
        await impl.save_state(
            session_id="resolved-session",
            state=state,
            message=message,
            model_output="ignored",
        )
        assert isinstance(exc, ValueError)

    assert lifespan.exit_args is not None
    assert lifespan.exit_args[0] is ValueError
    assert isinstance(lifespan.exit_args[1], ValueError)


@pytest.mark.asyncio
async def test_load_state_injects_model_recorded_on_session_tape(tmp_path: Path) -> None:
    """A model_switch event recorded on the session tape is restored into state on load."""
    _, impl, agent = _build_impl(tmp_path)
    session = agent.tape.session_tape("resolved-session", impl.framework.workspace)
    await session.append_event("model_switch", {"model": "openai:gpt-4o"})

    message = ChannelMessage(session_id="session", channel="cli", chat_id="room", content="hello")

    state = await impl.load_state(message=message, session_id="resolved-session")

    assert state["model"] == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_load_state_does_not_inject_model_for_unknown_session(tmp_path: Path) -> None:
    """A session with nothing recorded on its tape must not inherit any model (no leakage)."""
    _, impl, _ = _build_impl(tmp_path)

    message = ChannelMessage(session_id="session", channel="cli", chat_id="room", content="hello")

    state = await impl.load_state(message=message, session_id="fresh-session")

    assert "model" not in state


@pytest.mark.asyncio
async def test_recover_session_model_returns_latest_recorded(tmp_path: Path) -> None:
    """When several switches were recorded, the most recent one wins."""
    _, impl, agent = _build_impl(tmp_path)
    session = agent.tape.session_tape("resolved-session", impl.framework.workspace)
    await session.append_event("model_switch", {"model": "openai:gpt-4o"})
    await session.append_event("model_switch", {"model": "anthropic:claude-3"})

    assert await impl._recover_session_model("resolved-session") == "anthropic:claude-3"


@pytest.mark.asyncio
async def test_build_prompt_marks_commands_and_prefixes_context(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    command = ChannelMessage(session_id="s", channel="cli", chat_id="room", content=",help")
    normal = ChannelMessage(session_id="s", channel="cli", chat_id="room", content="hello")

    command_prompt = await impl.build_prompt(command, session_id="s", state={})
    normal_prompt = await impl.build_prompt(normal, session_id="s", state={})

    assert command_prompt == ",help"
    assert command.kind == "command"
    prompt_lines = normal_prompt.splitlines()
    assert prompt_lines[0] == normal.context_str
    assert prompt_lines[2] == "hello"


@pytest.mark.asyncio
async def test_build_prompt_uses_system_timezone_for_context_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is not available on this platform")

    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        _, impl, _ = _build_impl(tmp_path)
        message = ChannelMessage(session_id="s", channel="cli", chat_id="room", content="hello")

        prompt = await impl.build_prompt(message, session_id="s", state={})

        date_line = prompt.splitlines()[1]
        assert date_line.startswith("---Date: ")
        assert date_line.endswith("+08:00---")
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        time.tzset()


@pytest.mark.asyncio
async def test_run_model_stream_delegates_to_agent(tmp_path: Path) -> None:
    _, impl, agent = _build_impl(tmp_path)
    state = {"context": "ctx"}

    stream = await impl.run_model_stream(prompt="prompt", session_id="session", state=state)
    events = [event async for event in stream]

    assert [(event.kind, event.data) for event in events] == [("text", {"delta": "agent-output"})]
    assert agent.run_stream_calls == [("session", "prompt", state, None)]
    assert agent.run_calls == []


@pytest.mark.asyncio
async def test_runtime_model_override_is_passed_to_agent(tmp_path: Path) -> None:
    _, impl, agent = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="session",
        channel="cli",
        chat_id="room",
        content="hello",
        context={"model": "anthropic:claude-sonnet-4-5"},
    )

    state = await impl.load_state(message=message, session_id="session")
    stream = await impl.run_model_stream(prompt="prompt", session_id="session", state=state)
    events = [event async for event in stream]

    assert [(event.kind, event.data) for event in events] == [("text", {"delta": "agent-output"})]
    assert agent.run_stream_calls == [("session", "prompt", state, "anthropic:claude-sonnet-4-5")]


@pytest.mark.asyncio
async def test_run_model_stream_forwards_state_model_override(tmp_path: Path) -> None:
    """state['model'] must be forwarded as the per-call model override."""
    _, impl, agent = _build_impl(tmp_path)
    state = {"model": "openai:gpt-4o"}

    await impl.run_model_stream(prompt="prompt", session_id="session", state=state)

    assert agent.run_stream_calls == [("session", "prompt", state, "openai:gpt-4o")]


@pytest.mark.asyncio
async def test_run_model_stream_passes_none_when_state_has_no_model(tmp_path: Path) -> None:
    """Without state['model'] the agent must fall back to its configured model."""
    _, impl, agent = _build_impl(tmp_path)
    state = {"context": "ctx"}

    await impl.run_model_stream(prompt="prompt", session_id="session", state=state)

    assert agent.run_stream_calls[-1][3] is None


def test_builtin_provides_model_runtime_options(tmp_path: Path, load_config) -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.delenv("BUB_MODEL", raising=False)
        monkeypatch.delenv("BUB_FALLBACK_MODELS", raising=False)
        config_file = load_config(
            """
model: openai:gpt-5
fallback_models:
  - anthropic:claude-sonnet-4-5
  - openai:gpt-5
""".strip()
        )
        _, impl, _ = _build_impl(tmp_path, config_file=config_file)

        options = impl.provide_model_options(session_id="session")

        assert options is not None
        assert options.current_model == "openai:gpt-5"
        assert [item.id for item in options.models] == ["openai:gpt-5", "anthropic:claude-sonnet-4-5"]


def test_system_prompt_appends_workspace_agents_file(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    (tmp_path / AGENTS_FILE_NAME).write_text("local rules", encoding="utf-8")

    result = impl.system_prompt(prompt="hello", state={"_runtime_workspace": str(tmp_path)})

    assert result == DEFAULT_SYSTEM_PROMPT + "\n\nlocal rules"


def test_system_prompt_ignores_missing_agents_file(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    result = impl.system_prompt(prompt="hello", state={"_runtime_workspace": str(tmp_path)})

    assert result == DEFAULT_SYSTEM_PROMPT + "\n\n"


def test_provide_channels_returns_cli_and_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, impl, agent = _build_impl(tmp_path)

    class DummyCliChannel:
        name = "cli"

        def __init__(self, on_receive, agent) -> None:
            self.on_receive = on_receive
            self.agent = agent

    class DummyTelegramChannel:
        name = "telegram"

        def __init__(self, on_receive) -> None:
            self.on_receive = on_receive

        @property
        def enabled(self) -> bool:
            return True

    import bub.channels.cli
    import bub.channels.telegram

    monkeypatch.setattr(bub.channels.cli, "CliChannel", DummyCliChannel)
    monkeypatch.setattr(bub.channels.telegram, "TelegramChannel", DummyTelegramChannel)

    def message_handler(message) -> None:
        return None

    channels = impl.provide_channels(message_handler)

    assert [channel.name for channel in channels] == ["telegram", "cli"]
    assert channels[0].on_receive is message_handler
    assert channels[1].on_receive is message_handler
    assert channels[1].agent is agent


@pytest.mark.asyncio
async def test_on_error_dispatches_outbound_message(tmp_path: Path) -> None:
    framework, impl, _ = _build_impl(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    async def call_many(name: str, **kwargs: object) -> list[object]:
        calls.append((name, kwargs))
        return []

    framework._hook_runtime.call_many = call_many  # type: ignore[method-assign]

    await impl.on_error(stage="turn", error=RuntimeError("bad"), message={"channel": "cli", "chat_id": "room"})

    assert len(calls) == 1
    hook_name, kwargs = calls[0]
    outbound = kwargs["message"]
    assert hook_name == "dispatch_outbound"
    assert outbound.channel == "cli"
    assert outbound.chat_id == "room"
    assert outbound.kind == "error"
    assert outbound.content == "An error occurred at stage 'turn': bad"


@pytest.mark.asyncio
async def test_dispatch_outbound_uses_framework_router(tmp_path: Path) -> None:
    framework, impl, _ = _build_impl(tmp_path)
    dispatched: list[object] = []

    async def dispatch_via_channel_router(message: object) -> bool:
        dispatched.append(message)
        return True

    framework.dispatch_via_channel_router = dispatch_via_channel_router  # type: ignore[method-assign]
    outbound = {"session_id": "session", "channel": "cli", "chat_id": "room", "content": "hello"}

    result = await impl.dispatch_outbound(outbound)

    assert result is True
    assert dispatched == [outbound]


def test_render_outbound_preserves_message_metadata(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    rendered = impl.render_outbound(
        message={"channel": "telegram", "chat_id": "room", "kind": "command", "output_channel": "cli"},
        session_id="session",
        state={},
        model_output="result",
    )

    assert len(rendered) == 1
    outbound = rendered[0]
    assert outbound.session_id == "session"
    assert outbound.channel == "telegram"
    assert outbound.chat_id == "room"
    assert outbound.output_channel == "cli"
    assert outbound.kind == "command"
    assert outbound.content == "result"


def test_provide_tape_store_uses_bub_home_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, impl, _ = _build_impl(tmp_path)
    monkeypatch.setenv("BUB_HOME", str(tmp_path))

    store = impl.provide_tape_store()

    assert isinstance(store, FileTapeStore)
    assert store._directory == tmp_path / "tapes"
