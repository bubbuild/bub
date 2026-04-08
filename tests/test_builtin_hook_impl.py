from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bub.builtin.hook_impl import AGENTS_FILE_NAME, DEFAULT_SYSTEM_PROMPT, BuiltinImpl
from bub.builtin.store import FileTapeStore
from bub.channels.message import ChannelMessage
from bub.framework import BubFramework


class RecordingLifespan:
    def __init__(self) -> None:
        self.entered = False
        self.exit_args: tuple[object, object, object] | None = None

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exit_args = (exc_type, exc, traceback)


class FakeAgent:
    def __init__(self, home: Path) -> None:
        self.settings = SimpleNamespace(home=home)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def run(self, *, session_id: str, prompt: str, state: dict[str, object]) -> str:
        self.calls.append((session_id, prompt, state))
        return "agent-output"


def _raise_value_error() -> None:
    raise ValueError("boom")


def _build_impl(tmp_path: Path) -> tuple[BubFramework, BuiltinImpl, FakeAgent]:
    framework = BubFramework()
    impl = BuiltinImpl(framework)
    agent = FakeAgent(tmp_path)
    impl.agent = agent  # type: ignore[assignment]
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
    _, impl, _ = _build_impl(tmp_path)
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
    assert state["_runtime_agent"] is impl.agent
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
async def test_run_model_delegates_to_agent(tmp_path: Path) -> None:
    _, impl, agent = _build_impl(tmp_path)
    state = {"context": "ctx"}

    result = await impl.run_model(prompt="prompt", session_id="session", state=state)

    assert result == "agent-output"
    assert agent.calls == [("session", "prompt", state)]


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

    async def dispatch_via_router(message: object) -> bool:
        dispatched.append(message)
        return True

    framework.dispatch_via_router = dispatch_via_router  # type: ignore[method-assign]
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


def test_provide_tape_store_uses_agent_home_directory(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    store = impl.provide_tape_store()

    assert isinstance(store, FileTapeStore)
    assert store._directory == tmp_path / "tapes"
