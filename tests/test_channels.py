from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import re
import select
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bub.channels.base import Channel, Interface, Lifecycle
from bub.channels.cli import CliChannel
from bub.channels.cli.renderer import CliRenderer
from bub.channels.handler import BufferedMessageHandler
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.channels.telegram import BubMessageFilter, TelegramChannel, TelegramMessageParser
from bub.runtime import StreamEvent
from bub.turn_admission import AdmitDecision, SessionTurnController, SteeringBuffer, TurnSnapshot

ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Za-z])")


def _load_channel_config(
    load_config,
    *,
    enabled_channels: str = "all",
    stream_output: bool = False,
    telegram_value: str = "",
) -> None:
    content = f"""
enabled_channels: {enabled_channels}
stream_output: {str(stream_output).lower()}
telegram:
  token: {telegram_value!r}
""".strip()
    load_config(content)


def _read_pty_until_exit(master_fd: int, process: subprocess.Popen[bytes], *, timeout: float = 3.0) -> bytes:
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            with contextlib.suppress(OSError):
                chunks.append(os.read(master_fd, 65536))
            break
        readable, _, _ = select.select([master_fd], [], [], 0.05)
        if not readable:
            continue
        try:
            chunk = os.read(master_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _plain_terminal_text(raw: bytes) -> str:
    text = raw.decode(errors="replace")
    return ANSI_RE.sub("", text).replace("\r", "\n")


class _FakeChannelMixin:
    def __init__(self, name: str, *, needs_debounce: bool = False) -> None:
        self.name = name
        self._needs_debounce = needs_debounce
        self.sent: list[ChannelMessage] = []
        self.started = False
        self.stopped = False

    @property
    def needs_debounce(self) -> bool:
        return self._needs_debounce

    async def start(self, stop_event: asyncio.Event) -> None:
        self.started = True
        self.stop_event = stop_event

    async def stop(self) -> None:
        self.stopped = True

    @property
    def enabled(self) -> bool:
        return True

    async def send(self, message: ChannelMessage) -> None:
        self.sent.append(message)


class FakeChannel(_FakeChannelMixin, Channel):
    pass


class FakeInterfaceChannel(_FakeChannelMixin, Interface):
    pass


class FakeLifecycleChannel(_FakeChannelMixin, Lifecycle):
    pass


class FakeFramework:
    def __init__(self, channels: dict[str, Channel]) -> None:
        self._channels = channels
        self.router = None
        self.process_calls: list[tuple[ChannelMessage, bool]] = []
        self.admission_decisions: list[AdmitDecision | None] = []
        self.admission_calls: list[tuple[str, ChannelMessage, object]] = []
        self._steering_buffers: dict[str, SteeringBuffer] = {}
        self.resolved_sessions: dict[str, str] = {}
        self._hook_runtime = SimpleNamespace(notify_error=self._notify_error)
        self.running_entries = 0
        self.running_exits = 0

    def get_channels(self, message_handler):
        self.message_handler = message_handler
        return self._channels

    @contextlib.asynccontextmanager
    async def running(self):
        self.running_entries += 1
        try:
            yield
        finally:
            self.running_exits += 1

    def bind_outbound_router(self, router) -> None:
        self.router = router

    async def process_inbound(self, message: ChannelMessage, stream_output: bool = False):
        self.process_calls.append((message, stream_output))
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()
        return None

    async def admit_message(self, *, session_id: str, message: ChannelMessage, turn):
        self.admission_calls.append((session_id, message, turn))
        if self.admission_decisions:
            return self.admission_decisions.pop(0)
        return None

    async def resolve_session(self, message: ChannelMessage) -> str:
        return self.resolved_sessions.get(message.session_id, message.session_id)

    def steering(self, session_id: str) -> SteeringBuffer:
        buffer = self._steering_buffers.get(session_id)
        if buffer is None:
            buffer = SteeringBuffer(session_id=session_id)
            self._steering_buffers[session_id] = buffer
        return buffer

    def clear_steering(self, session_id: str) -> None:
        self._steering_buffers.pop(session_id, None)

    async def _notify_error(self, *, stage: str, error: Exception, message: ChannelMessage | None) -> None:
        return None


def _message(
    content: str,
    *,
    channel: str = "telegram",
    session_id: str = "telegram:chat",
    chat_id: str = "chat",
    is_active: bool = False,
    kind: str = "normal",
) -> ChannelMessage:
    return ChannelMessage(
        session_id=session_id,
        channel=channel,
        chat_id=chat_id,
        content=content,
        is_active=is_active,
        kind=kind,
    )


class _FakeTelegramUpdater:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    async def start_polling(self, **kwargs) -> None:
        self.kwargs = kwargs


class _FakeTelegramApp:
    def __init__(self) -> None:
        self.updater = _FakeTelegramUpdater()
        self.handlers: list[object] = []

    def add_handler(self, handler: object) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        return

    async def start(self) -> None:
        return


class _FakeTelegramBuilder:
    def __init__(self) -> None:
        self.app = _FakeTelegramApp()
        self.request: object | None = None
        self.proxy_value: str | None = None
        self.token_value: str | None = None

    def token(self, token: str) -> _FakeTelegramBuilder:
        self.token_value = token
        return self

    def get_updates_request(self, request: object) -> _FakeTelegramBuilder:
        self.request = request
        return self

    def proxy(self, proxy: str) -> _FakeTelegramBuilder:
        self.proxy_value = proxy
        return self

    def get_updates_proxy(self, _proxy: str) -> _FakeTelegramBuilder:
        raise AssertionError("get_updates_proxy should not be called when get_updates_request is already set")

    def build(self) -> _FakeTelegramApp:
        return self.app


def _telegram_proxy_config() -> str:
    return """
telegram:
  token: "test-token"
  proxy: "http://127.0.0.1:1087"
""".strip()


@pytest.mark.asyncio
async def test_buffered_handler_passes_commands_through_immediately() -> None:
    handled: list[str] = []

    async def receive(message: ChannelMessage) -> None:
        handled.append(message.content)

    handler = BufferedMessageHandler(
        receive,
        active_time_window=10,
        max_wait_seconds=10,
        debounce_seconds=0.01,
    )

    await handler(_message(",help"))

    assert handled == [",help"]


@pytest.mark.asyncio
async def test_channel_manager_dispatch_uses_output_channel_and_preserves_metadata(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="cli")
    cli_channel = FakeChannel("cli")
    manager = ChannelManager(FakeFramework({"cli": cli_channel}), enabled_channels=["cli"])

    result = await manager.dispatch_output({
        "session_id": "session",
        "channel": "telegram",
        "output_channel": "cli",
        "chat_id": "room",
        "content": "hello",
        "kind": "command",
        "context": {"source": "test"},
    })

    assert result is True
    assert len(cli_channel.sent) == 1
    outbound = cli_channel.sent[0]
    assert outbound.channel == "cli"
    assert outbound.chat_id == "room"
    assert outbound.content == "hello"
    assert outbound.kind == "command"
    assert outbound.context["source"] == "test"


@pytest.mark.parametrize(
    ("enabled_channels", "expected_channels"),
    [
        (["all"], ["mcp.lifecycle", "manual.lifecycle", "telegram", "discord"]),
        (["cli"], ["cli", "mcp.lifecycle", "manual.lifecycle"]),
        (["mcp.lifecycle"], ["mcp.lifecycle"]),
        (["cli", "!mcp.lifecycle"], ["cli", "manual.lifecycle"]),
        (["all", "!mcp.lifecycle", "!telegram"], ["manual.lifecycle", "discord"]),
        (["mcp.lifecycle", "!mcp.lifecycle"], []),
    ],
)
def test_channel_manager_selects_channels_by_runtime_role(
    load_config, enabled_channels: list[str], expected_channels: list[str]
) -> None:
    _load_channel_config(load_config)
    channels = {
        "cli": FakeInterfaceChannel("cli"),
        "mcp.lifecycle": FakeLifecycleChannel("mcp.lifecycle"),
        "manual.lifecycle": FakeLifecycleChannel("manual.lifecycle"),
        "telegram": FakeChannel("telegram"),
        "discord": FakeChannel("discord"),
    }
    manager = ChannelManager(FakeFramework(channels), enabled_channels=enabled_channels)

    assert [channel.name for channel in manager.enabled_channels()] == expected_channels


def test_channel_manager_selects_real_channel_types(load_config) -> None:
    _load_channel_config(load_config, telegram_value="test-token")
    cli = CliChannel.__new__(CliChannel)
    telegram = TelegramChannel(lambda message: None)
    manager = ChannelManager(
        FakeFramework({"cli": cli, "telegram": telegram}),
        enabled_channels=["all"],
    )

    assert [channel.name for channel in manager.enabled_channels()] == ["telegram"]


@pytest.mark.asyncio
async def test_cli_channel_accepts_input_while_previous_message_is_running() -> None:
    received: list[ChannelMessage] = []

    class FakePrompt:
        def __init__(self) -> None:
            self.inputs = iter(["first", "second", ",quit"])
            self.refresh_intervals: list[float | None] = []
            self.messages: list[str] = []
            self.received_callables: list[bool] = []

        async def prompt_async(self, message, *, refresh_interval=None):
            self.refresh_intervals.append(refresh_interval)
            self.received_callables.append(callable(message))
            rendered = message() if callable(message) else message
            self.messages.append("".join(part for _, part in rendered))
            return next(self.inputs)

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)

    channel = CliChannel.__new__(CliChannel)
    channel._on_receive = on_receive
    channel._stop_event = asyncio.Event()
    channel._message_template = {
        "chat_id": "cli_chat",
        "channel": "cli",
        "session_id": "cli_session",
    }
    channel._agent = SimpleNamespace(settings=SimpleNamespace(model="test-model"))
    channel._workspace = Path.cwd()
    channel._mode = "agent"
    channel._llm_loop_running = False
    channel._prompt = FakePrompt()
    echoed: list[tuple[str, str]] = []
    channel._renderer = SimpleNamespace(
        welcome=lambda **kwargs: None,
        info=lambda message: None,
        input_echo=lambda prompt, text: echoed.append((prompt, text)),
    )
    channel._refresh_tape_info = _async_return(None)

    await asyncio.wait_for(channel._main_loop(), timeout=1)

    import bub.channels.cli as cli_module

    assert [message.content for message in received] == ["first", "second"]
    assert channel._prompt.refresh_intervals == [cli_module._PROMPT_REFRESH_INTERVAL] * 3
    assert channel._prompt.received_callables == [True, True, True]
    assert "Generating\n" not in channel._prompt.messages[0]
    assert "Generating\n" in channel._prompt.messages[1]
    assert echoed == [(f"{Path.cwd().name} > ", "first"), (f"{Path.cwd().name} > ", "second")]
    assert all(message.lifespan is not None for message in received)


def test_cli_channel_build_prompt_erases_submitted_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakePromptSession:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("bub.channels.cli.PromptSession", FakePromptSession)
    channel = CliChannel.__new__(CliChannel)
    channel._mode = "agent"
    channel._expand_thinking = False
    channel._agent = SimpleNamespace(settings=SimpleNamespace(model="test-model"))
    channel._last_tape_info = None

    prompt = channel._build_prompt(tmp_path)

    assert isinstance(prompt, FakePromptSession)
    assert captured["erase_when_done"] is True


def test_cli_channel_generating_spinner_renders_above_input_not_toolbar(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = CliChannel.__new__(CliChannel)
    channel._llm_loop_running = True
    channel._mode = "agent"
    channel._expand_thinking = False
    channel._last_tape_info = None
    channel._agent = SimpleNamespace(settings=SimpleNamespace(model="test-model"))

    prompt_text = "".join(part for _, part in channel._prompt_message())
    toolbar_text = "".join(part for _, part in channel._render_bottom_toolbar())

    assert "\n" in prompt_text
    assert "Generating\n" in prompt_text
    assert prompt_text.endswith(f"{Path.cwd().name} > ")
    assert "Generating" not in toolbar_text

    import bub.channels.cli as cli_module

    monkeypatch.setattr(cli_module, "monotonic", lambda: 0.0)
    first_frame = "".join(part for _, part in channel._prompt_message())
    monkeypatch.setattr(cli_module, "monotonic", lambda: 0.2)
    second_frame = "".join(part for _, part in channel._prompt_message())

    assert first_frame != second_frame


def test_cli_channel_admit_message_queues_follow_up_when_turn_is_running() -> None:
    channel = CliChannel.__new__(CliChannel)
    turn = TurnSnapshot(
        session_id="cli_session",
        is_running=True,
        running_count=1,
        pending_count=0,
        steering_count=0,
    )

    decision = channel.admit_message(
        session_id="cli_session",
        message=_message("second", channel="cli", session_id="cli_session"),
        turn=turn,
    )

    assert decision == AdmitDecision("follow_up", reason="cli session is already generating")


@pytest.mark.asyncio
async def test_channel_manager_on_receive_uses_buffer_for_debounced_channel(
    monkeypatch: pytest.MonkeyPatch, load_config
) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    telegram = FakeChannel("telegram", needs_debounce=True)
    manager = ChannelManager(FakeFramework({"telegram": telegram}), enabled_channels=["telegram"])
    calls: list[ChannelMessage] = []

    class StubBufferedMessageHandler:
        def __init__(
            self, handler, *, active_time_window: float, max_wait_seconds: float, debounce_seconds: float
        ) -> None:
            self.handler = handler
            self.settings = (active_time_window, max_wait_seconds, debounce_seconds)

        async def __call__(self, message: ChannelMessage) -> None:
            calls.append(message)

    import bub.channels.manager as manager_module

    monkeypatch.setattr(manager_module, "BufferedMessageHandler", StubBufferedMessageHandler)

    message = _message("hello", channel="telegram")
    await manager.on_receive(message)
    await manager.on_receive(message)

    assert calls == [message, message]
    assert message.session_id in manager._session_handlers
    assert isinstance(manager._session_handlers[message.session_id], StubBufferedMessageHandler)


@pytest.mark.asyncio
async def test_channel_manager_shutdown_cancels_tasks_and_stops_enabled_channels(load_config) -> None:
    _load_channel_config(load_config)
    telegram = FakeChannel("telegram")
    cli = FakeInterfaceChannel("cli")
    manager = ChannelManager(FakeFramework({"telegram": telegram, "cli": cli}), enabled_channels=["all"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {task}

    await manager.shutdown()

    assert task.cancelled()
    assert telegram.stopped is True
    assert cli.stopped is False


@pytest.mark.asyncio
async def test_channel_manager_listen_and_run_passes_stream_output_setting(
    monkeypatch: pytest.MonkeyPatch, load_config
) -> None:
    _load_channel_config(load_config, enabled_channels="telegram", stream_output=True)
    framework = FakeFramework({"telegram": FakeChannel("telegram")})

    import bub.channels.manager as manager_module

    manager = ChannelManager(framework)
    calls = 0
    spawned_coroutines = []
    original_create_task = manager_module.asyncio.create_task

    class DummyTask:
        def add_done_callback(self, callback) -> None:
            return None

        def cancel(self) -> None:
            return None

        def exception(self):
            return None

    def create_task(coro):
        spawned_coroutines.append(coro)
        return DummyTask()

    async def wait_until_stopped(awaitable, current_stop_event):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await awaitable
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise asyncio.CancelledError

    async def shutdown() -> None:
        return None

    manager.shutdown = shutdown  # type: ignore[method-assign]
    monkeypatch.setattr(manager_module.asyncio, "create_task", create_task)
    monkeypatch.setattr(manager_module, "wait_until_stopped", wait_until_stopped)

    listen_task = original_create_task(manager.listen_and_run())
    await asyncio.sleep(0)
    await manager.on_receive(_message("hello", channel="telegram"))
    await listen_task
    assert len(spawned_coroutines) == 1
    await spawned_coroutines[0]

    assert len(framework.process_calls) == 1
    message, stream_output = framework.process_calls[0]
    assert message.content == "hello"
    assert stream_output is True
    assert framework.running_entries == 1
    assert framework.running_exits == 1


@pytest.mark.asyncio
async def test_channel_manager_quit_cancels_only_matching_session_tasks(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    manager = ChannelManager(FakeFramework({"telegram": FakeChannel("telegram")}), enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    target_task = asyncio.create_task(never_finish())
    other_task = asyncio.create_task(never_finish())
    manager._controller("session:target").active_tasks = {target_task}
    manager._controller("session:other").active_tasks = {other_task}

    await manager.quit("session:target")

    assert target_task.cancelled()
    assert "session:target" not in manager._session_controllers
    assert other_task.cancelled() is False
    assert manager._session_controllers["session:other"].active_tasks == {other_task}

    other_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await other_task


@pytest.mark.asyncio
async def test_channel_manager_quit_skips_current_task(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    manager = ChannelManager(FakeFramework({"telegram": FakeChannel("telegram")}), enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    current_task = asyncio.current_task()
    assert current_task is not None
    target_task = asyncio.create_task(never_finish())
    controller = manager._controller("session:target")
    controller.active_tasks = {current_task, target_task}

    await manager.quit("session:target")

    assert current_task.cancelled() is False
    assert target_task.cancelled()
    assert controller.active_tasks == {current_task}


@pytest.mark.asyncio
async def test_channel_manager_done_callback_handles_cancelled_task(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    manager = ChannelManager(FakeFramework({"telegram": FakeChannel("telegram")}), enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(never_finish())
    manager._controller("session:target").active_tasks = {task}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    manager._on_task_done("session:target", task)

    assert "session:target" not in manager._session_controllers


@pytest.mark.asyncio
async def test_channel_manager_admission_default_keeps_concurrent_processing(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("second"))

    assert admitted is True
    session_id, message, turn = framework.admission_calls[0]
    assert session_id == "telegram:chat"
    assert message.content == "second"
    assert turn.is_running is True
    assert turn.running_count == 1
    assert turn.pending_count == 0
    assert turn.steering_count == 0

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_uses_resolved_session_for_control(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.resolved_sessions["telegram:raw"] = "tenant:canonical"
    framework.admission_decisions.append(AdmitDecision("follow_up", reason="serial"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("tenant:canonical").active_tasks = {active}

    admitted = await manager._admit_message(_message("second", session_id="telegram:raw"))

    assert admitted is False
    assert framework.admission_calls[0][0] == "tenant:canonical"
    assert "telegram:raw" not in manager._session_controllers
    assert [message.content for message in manager._session_controllers["tenant:canonical"].pending_queue] == ["second"]

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_drop_discards_message(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision("drop", reason="busy"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("drop me"))

    assert admitted is False
    assert not manager._session_controllers["telegram:chat"].pending_queue

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_follow_up_queues_pending_message(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision("follow_up", reason="serial"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("queued"))

    assert admitted is False
    assert [message.content for message in manager._session_controllers["telegram:chat"].pending_queue] == ["queued"]

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_steer_promotes_undrained_messages_to_pending(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.extend([
        AdmitDecision("steer", reason="correction"),
        AdmitDecision("steer", reason="correction"),
    ])
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    done = asyncio.create_task(asyncio.sleep(0))
    controller = manager._controller("telegram:chat")
    controller.active_tasks = {done}
    controller.add_pending(_message("already waiting"))

    admitted = await manager._admit_message(_message("actually do this"))
    admitted_again = await manager._admit_message(_message("then this"))
    await done
    manager._on_task_done("telegram:chat", done)
    for _ in range(10):
        if len(framework.process_calls) == 3:
            break
        await asyncio.sleep(0)

    assert admitted is False
    assert admitted_again is False
    assert [message.content for message, _ in framework.process_calls] == [
        "actually do this",
        "then this",
        "already waiting",
    ]


@pytest.mark.asyncio
async def test_channel_manager_admission_steer_drain_acknowledges_ownership(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision("steer", reason="correction"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    done = asyncio.create_task(asyncio.sleep(0))
    controller = manager._controller("telegram:chat")
    controller.active_tasks = {done}

    admitted = await manager._admit_message(_message("consume me"))
    drained = framework.steering("telegram:chat").drain_nowait()
    await done
    manager._on_task_done("telegram:chat", done)

    assert admitted is False
    assert [message.content for message in drained] == ["consume me"]
    assert framework.process_calls == []


def test_turn_admission_queues_preserve_messages_without_capacity_policy() -> None:
    steering = SteeringBuffer(session_id="telegram:chat")

    steering.put_nowait(_message("one"))
    steering.put_nowait(_message("two"))
    steering.put_nowait(_message("three with a long body"))
    drained_one = steering.get_nowait()
    assert drained_one is not None
    assert drained_one.content == "one"
    assert [message.content for message in steering.drain_nowait()] == ["two", "three with a long body"]

    controller = SessionTurnController(session_id="telegram:chat", steering=SteeringBuffer(session_id="telegram:chat"))

    controller.add_pending(_message("one"))
    controller.add_pending(_message("two"))
    controller.add_pending(_message("three with a long body"))
    assert [message.content for message in controller.pending_queue] == ["one", "two", "three with a long body"]

    controller.add_pending_left(_message("priority"))
    assert [message.content for message in controller.pending_queue] == [
        "priority",
        "one",
        "two",
        "three with a long body",
    ]


def test_cli_channel_normalize_input_prefixes_shell_commands() -> None:
    channel = CliChannel.__new__(CliChannel)
    channel._mode = "shell"

    assert channel._normalize_input("ls") == ",ls"
    assert channel._normalize_input(",help") == ",help"


@pytest.mark.asyncio
async def test_cli_channel_stream_events_prints_stream_and_yields_events(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = CliChannel.__new__(CliChannel)
    heads: list[str] = []
    printed: list[tuple[str, str | None, bool | None]] = []
    channel._renderer = SimpleNamespace(print_head=heads.append)
    channel._expand_thinking = False
    monkeypatch.setattr(
        "bub.channels.cli.get_console",
        lambda: SimpleNamespace(
            print=lambda content, end=None, highlight=None: printed.append((content, end, highlight))
        ),
    )

    message = _message("ignored", channel="cli", kind="command", session_id="cli:1")

    async def source() -> asyncio.AsyncIterator[StreamEvent]:
        yield StreamEvent("text", {"delta": "  "})
        yield StreamEvent("text", {"delta": "hel"})
        yield StreamEvent("text", {"delta": "lo"})
        yield StreamEvent("final", {})

    yielded = [event async for event in channel.stream_events(message, source())]

    assert heads == ["command"]
    assert printed == [("hel\n", "", False), ("hello\n", "", False)]
    assert [event.kind for event in yielded] == ["text", "text", "final"]


def test_cli_stream_output_does_not_overlap_active_pty_prompt() -> None:
    script = textwrap.dedent(
        """
        import asyncio

        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        from rich.console import Console

        from bub.channels.cli import _StreamPrinter
        from bub.runtime import StreamEvent


        async def main():
            console = Console(force_terminal=True, color_system=None, width=80)
            printer = _StreamPrinter(
                console=console,
                print_head=lambda: console.print("Assistant >"),
                expand_thinking=False,
            )
            session = PromptSession(erase_when_done=True)

            async def stream():
                chunks = [
                    "春风一夜入江城\\n",
                    "细雨无声湿客",
                    "程\\n",
                    "莫问归帆何处",
                    "去\\n",
                    "明朝山色满",
                    "前庭",
                ]
                for index, chunk in enumerate(chunks):
                    await asyncio.sleep(0.03)
                    await printer.render(StreamEvent("text", {"delta": chunk}))
                    if index == 3:
                        await printer.commit_live_text()
                        console.print("bub > steer now")
                await asyncio.sleep(0.03)
                await printer.render(StreamEvent("final", {}))

            task = asyncio.create_task(stream())
            with patch_stdout(raw=True):
                await session.prompt_async(
                    lambda: [("", "\\n* Generating\\nbub > ")],
                    refresh_interval=0.02,
                )
            await task


        asyncio.run(main())
        """
    )
    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{Path.cwd() / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=Path.cwd(),
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        time.sleep(0.25)
        os.write(master_fd, b"next\n")
        raw_output = _read_pty_until_exit(master_fd, process)
    finally:
        if process.poll() is None:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
        os.close(master_fd)

    assert process.wait(timeout=1) == 0, raw_output.decode(errors="replace")
    output = _plain_terminal_text(raw_output)

    assert "春风一夜入江城" in output
    assert "细雨无声湿客程" in output
    assert "莫问归帆何处" in output
    assert "去" in output
    assert "明朝山色满前庭" in output
    assert "bub > steer now" in output
    assert "明朝山色满前庭bub >" not in output
    assert "明朝山色满前庭* Generating" not in output


@pytest.mark.asyncio
async def test_cli_channel_input_echo_commits_active_stream_line() -> None:
    channel = CliChannel.__new__(CliChannel)
    calls: list[str] = []

    class FakeStreamPrinter:
        async def commit_live_text(self) -> None:
            calls.append("commit")

    channel._stream_printer = FakeStreamPrinter()
    channel._mode = "agent"
    channel._renderer = SimpleNamespace(input_echo=lambda prompt, text: calls.append(f"echo:{text}"))

    await channel._echo_input("steer now")

    assert calls == ["commit", "echo:steer now"]


@pytest.mark.asyncio
async def test_cli_channel_collapsed_reasoning_does_not_start_status_spinner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = CliChannel.__new__(CliChannel)
    channel._renderer = SimpleNamespace(print_head=lambda kind: None)
    channel._expand_thinking = False
    printed: list[object] = []

    def status(*args, **kwargs):
        raise AssertionError("status spinner should not start while prompt is active")

    monkeypatch.setattr(
        "bub.channels.cli.get_console",
        lambda: SimpleNamespace(
            print=lambda content, end=None, highlight=None: printed.append(content),
            status=status,
        ),
    )

    message = _message("ignored", channel="cli", kind="normal", session_id="cli:1")

    async def source() -> asyncio.AsyncIterator[StreamEvent]:
        yield StreamEvent("reasoning", {"delta": "hidden"})
        yield StreamEvent("text", {"delta": "hello"})
        yield StreamEvent("final", {})

    yielded = [event async for event in channel.stream_events(message, source())]

    assert [event.kind for event in yielded] == ["reasoning", "text", "final"]
    assert printed
    assert any("hello" in str(item) for item in printed)


def test_cli_channel_history_file_uses_workspace_hash(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"

    result = CliChannel._history_file(home, workspace)

    assert result.parent == home / "history"
    assert result.suffix == ".history"


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("command", "[cyan bold]Command >[/]"),
        ("error", "[red bold]Error >[/]"),
        ("normal", "[blue bold]Assistant >[/]"),
    ],
)
def test_cli_renderer_print_head_uses_message_kind(kind: str, expected: str) -> None:
    printed: list[tuple[str, bool | None]] = []

    def print_message(message: str, *, new_line_start: bool | None = None) -> None:
        printed.append((message, new_line_start))

    renderer = CliRenderer(SimpleNamespace(print=print_message))  # type: ignore[arg-type]

    renderer.print_head(kind)  # type: ignore[arg-type]

    assert printed == [(expected, True)]


def test_bub_message_filter_accepts_private_messages() -> None:
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), text="hello")

    assert BubMessageFilter().filter(message) is True


def test_bub_message_filter_requires_group_mention_or_reply() -> None:
    bot = SimpleNamespace(id=1, username="BubBot")
    message = SimpleNamespace(
        chat=SimpleNamespace(type="group"),
        text="hello team",
        caption=None,
        entities=[],
        caption_entities=[],
        reply_to_message=None,
        get_bot=lambda: bot,
    )

    assert BubMessageFilter().filter(message) is False


def test_bub_message_filter_accepts_group_mention() -> None:
    bot = SimpleNamespace(id=1, username="BubBot")
    message = SimpleNamespace(
        chat=SimpleNamespace(type="group"),
        text="ping @bubbot",
        caption=None,
        entities=[SimpleNamespace(type="mention", offset=5, length=7)],
        caption_entities=[],
        reply_to_message=None,
        get_bot=lambda: bot,
    )

    assert BubMessageFilter().filter(message) is True


@pytest.mark.asyncio
async def test_telegram_channel_send_extracts_json_message_and_skips_blank(load_config) -> None:
    _load_channel_config(load_config, telegram_value="test-token")
    channel = TelegramChannel(lambda message: None)
    sent: list[tuple[str, str]] = []

    async def send_message(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))

    await channel.send(_message('{"message":"hello"}', chat_id="42"))
    await channel.send(_message("   ", chat_id="42"))

    assert sent == [("42", "hello")]


@pytest.mark.asyncio
async def test_telegram_channel_start_with_proxy_does_not_call_get_updates_proxy(
    monkeypatch: pytest.MonkeyPatch, load_config
) -> None:
    load_config(_telegram_proxy_config())
    fake_builder = _FakeTelegramBuilder()
    monkeypatch.setattr("bub.channels.telegram.Application.builder", lambda: fake_builder)

    channel = TelegramChannel(lambda message: None)
    await channel.start(asyncio.Event())

    assert fake_builder.proxy_value == "http://127.0.0.1:1087"
    assert fake_builder.request is not None
    assert fake_builder.app.updater.kwargs == {"drop_pending_updates": True, "allowed_updates": ["message"]}


@pytest.mark.asyncio
async def test_telegram_channel_build_message_returns_command_directly(load_config) -> None:
    _load_channel_config(load_config, telegram_value="test-token")
    channel = TelegramChannel(lambda message: None)
    channel._parser = SimpleNamespace(parse=_async_return((",help", {"type": "text"})), get_reply=_async_return(None))

    message = SimpleNamespace(chat_id=42)

    result = await channel._build_message(message)

    assert result.channel == "telegram"
    assert result.chat_id == "42"
    assert result.content == ",help"
    assert result.output_channel == "telegram"


@pytest.mark.asyncio
async def test_telegram_channel_build_message_wraps_payload_and_disables_outbound(
    monkeypatch: pytest.MonkeyPatch, load_config
) -> None:
    _load_channel_config(load_config, telegram_value="test-token")
    channel = TelegramChannel(lambda message: None)
    parser = SimpleNamespace(
        parse=_async_return(("hello", {"type": "text", "sender_id": "7"})),
        get_reply=_async_return({"message": "prev", "type": "text"}),
    )
    channel._parser = parser
    monkeypatch.setattr("bub.channels.telegram.MESSAGE_FILTER.filter", lambda message: True)

    message = SimpleNamespace(chat_id=42)

    result = await channel._build_message(message)

    assert result.output_channel == "null"
    assert result.is_active is True
    assert '"message": "hello"' in result.content
    assert '"reply_to_message"' in result.content
    assert result.lifespan is not None


@pytest.mark.asyncio
async def test_telegram_message_parser_extracts_formatted_links() -> None:
    parser = TelegramMessageParser()
    message = SimpleNamespace(
        text="Docs and https://example.com",
        caption=None,
        entities=[
            SimpleNamespace(type="text_link", url="https://docs.example.com"),
            SimpleNamespace(type="url", offset=9, length=19),
        ],
        caption_entities=[],
        message_id=1,
        from_user=SimpleNamespace(username="alice", full_name="Alice", id=7, is_bot=False),
        date=datetime(2026, 3, 11),
    )

    content, metadata = await parser.parse(message)

    assert content == "Docs and https://example.com"
    assert metadata["links"] == ["https://docs.example.com", "https://example.com"]


@pytest.mark.asyncio
async def test_telegram_message_parser_extracts_links_from_caption_entities() -> None:
    parser = TelegramMessageParser()
    message = SimpleNamespace(
        text=None,
        caption="See portal",
        entities=[],
        caption_entities=[SimpleNamespace(type="text_link", url="https://portal.example.com")],
        message_id=2,
        from_user=SimpleNamespace(username="alice", full_name="Alice", id=7, is_bot=False),
        date=datetime(2026, 3, 11),
        photo=[SimpleNamespace(file_id="file-1", file_size=3, width=1, height=1)],
    )

    async def fake_download_media(file_id: str, file_size: int) -> bytes:
        assert file_id == "file-1"
        assert file_size == 3
        return b"img"

    parser._download_media = fake_download_media  # type: ignore[method-assign]

    _content, metadata = await parser.parse(message)

    assert metadata["links"] == ["https://portal.example.com"]


def _async_return(value):
    async def runner(*args, **kwargs):
        return value

    return runner
