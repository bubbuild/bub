from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from republic import StreamEvent

from bub.channels.cli import CliChannel
from bub.channels.cli import renderer as cli_renderer
from bub.channels.cli.renderer import CliRenderer
from bub.channels.handler import BufferedMessageHandler
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.channels.telegram import BubMessageFilter, TelegramChannel, TelegramMessageParser
from bub.turn_admission import AdmitAction, AdmitDecision, SessionTurnController, SteeringBuffer, TurnControl


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


class FakeChannel:
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


class FakeFramework:
    def __init__(self, channels: dict[str, FakeChannel]) -> None:
        self._channels = channels
        self.router = None
        self.process_calls: list[tuple[ChannelMessage, bool]] = []
        self.admission_decisions: list[AdmitDecision | None] = []
        self.admission_calls: list[tuple[str, ChannelMessage, object]] = []
        self._turn_controls: dict[str, TurnControl] = {}
        self._supports_steering = True
        self._hook_runtime = SimpleNamespace(notify_error=self._notify_error)
        self.resolved_sessions: dict[str, str] = {}

    def get_channels(self, message_handler):
        self.message_handler = message_handler
        return self._channels

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

    async def _notify_error(self, *, stage: str, error: Exception, message: ChannelMessage | None) -> None:
        return None

    def supports_steering(self) -> bool:
        return self._supports_steering

    def turn_control(self, session_id: str) -> TurnControl:
        control = self._turn_controls.get(session_id)
        if control is None:
            control = TurnControl(session_id=session_id, buffer=SteeringBuffer())
            self._turn_controls[session_id] = control
        return control

    def clear_turn_control(self, session_id: str) -> None:
        self._turn_controls.pop(session_id, None)

    def inject_turn_message(self, session_id: str, message: ChannelMessage) -> bool:
        return self.turn_control(session_id).inject(message)

    def request_turn_cancel(self, session_id: str) -> None:
        self.turn_control(session_id).cancel()

    def reset_turn_cancel(self, session_id: str) -> None:
        self.turn_control(session_id).reset_cancel()


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


def test_channel_manager_enabled_channels_excludes_cli_from_all(load_config) -> None:
    _load_channel_config(load_config)
    channels = {"cli": FakeChannel("cli"), "telegram": FakeChannel("telegram"), "discord": FakeChannel("discord")}
    manager = ChannelManager(FakeFramework(channels), enabled_channels=["all"])

    assert [channel.name for channel in manager.enabled_channels()] == ["telegram", "discord"]


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
    cli = FakeChannel("cli")
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

        def set_name(self, name: str) -> None:
            self.name = name

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
async def test_channel_manager_quit_drops_pending_messages(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    controller = manager._controller("telegram:chat")
    controller.active_tasks = {active}
    controller.add_pending(_message("queued"))

    await manager.quit("telegram:chat")
    await asyncio.sleep(0)

    assert active.cancelled()
    assert framework.process_calls == []
    assert "telegram:chat" not in manager._session_controllers


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
    assert len(framework.admission_calls) == 1
    session_id, message, turn = framework.admission_calls[0]
    assert session_id == "telegram:chat"
    assert message.content == "second"
    assert turn.is_running is True
    assert turn.running_count == 1
    assert turn.pending_count == 0
    assert turn.steering_count == 0
    assert turn.supports_steering is True

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_uses_resolved_session_for_control(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.resolved_sessions["telegram:raw"] = "tenant:canonical"
    framework.admission_decisions.append(AdmitDecision(AdmitAction.WAIT, reason="serial"))
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
async def test_channel_manager_admission_hook_sees_idle_turn_snapshot(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    admitted = await manager._admit_message(_message("first"))

    assert admitted is True
    assert len(framework.admission_calls) == 1
    _, _, turn = framework.admission_calls[0]
    assert turn.is_running is False
    assert turn.running_count == 0
    assert "telegram:chat" not in manager._session_controllers


@pytest.mark.asyncio
async def test_channel_manager_admission_inject_on_idle_turn_falls_back_to_process(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision(AdmitAction.INJECT, reason="no running turn"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    admitted = await manager._admit_message(_message("first"))

    assert admitted is True
    assert framework.turn_control("telegram:chat").drain_injected() == []


@pytest.mark.asyncio
async def test_channel_manager_admission_wait_queues_pending_message(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision(AdmitAction.WAIT, reason="serial"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("queued"))

    assert admitted is False
    controller = manager._session_controllers["telegram:chat"]
    assert [message.content for message in controller.pending_queue] == ["queued"]

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_drop_discards_message(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision(AdmitAction.DROP, reason="busy"))
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
async def test_channel_manager_admission_inject_adds_to_steering_buffer(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision(AdmitAction.INJECT, reason="correction"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("actually do this"))

    assert admitted is False
    injected = framework.turn_control("telegram:chat").drain_injected()
    assert [message.content for message in injected] == ["actually do this"]

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_inject_falls_back_to_wait_without_steering_support(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework._supports_steering = False
    framework.admission_decisions.append(AdmitDecision(AdmitAction.INJECT, reason="correction"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("queue this"))

    assert admitted is False
    assert framework.turn_control("telegram:chat").drain_injected() == []
    assert [message.content for message in manager._session_controllers["telegram:chat"].pending_queue] == [
        "queue this"
    ]

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_cancel_and_process_requests_cancel_and_queues_replacement(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    framework.admission_decisions.append(AdmitDecision(AdmitAction.CANCEL_AND_PROCESS, reason="replace"))
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("replacement"))

    assert admitted is False
    assert active.cancelled() is False
    assert framework.turn_control("telegram:chat").is_cancelled is True
    assert [message.content for message in manager._session_controllers["telegram:chat"].pending_queue] == [
        "replacement"
    ]

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


@pytest.mark.asyncio
async def test_channel_manager_admission_hook_error_falls_back_to_process(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})

    async def broken_admit_message(*, session_id: str, message: ChannelMessage, turn):
        raise RuntimeError("bad admission")

    framework.admit_message = broken_admit_message  # type: ignore[method-assign]
    manager = ChannelManager(framework, enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    active = asyncio.create_task(never_finish())
    manager._controller("telegram:chat").active_tasks = {active}

    admitted = await manager._admit_message(_message("second"))

    assert admitted is True

    active.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await active


def test_steering_buffer_is_bounded_by_count_and_bytes() -> None:
    buffer = SteeringBuffer(max_size=2, max_bytes=5)

    assert buffer.put_nowait(_message("one")) is True
    assert buffer.put_nowait(_message("two")) is True
    assert buffer.put_nowait(_message("sixsix")) is False
    assert buffer.put_nowait(_message("三")) is True

    assert [message.content for message in buffer.drain_nowait()] == ["三"]


def test_session_turn_controller_pending_queue_is_bounded_by_count_and_bytes() -> None:
    control = TurnControl(session_id="telegram:chat", buffer=SteeringBuffer())
    controller = SessionTurnController(
        session_id="telegram:chat",
        steering=control,
        max_pending=2,
        max_pending_bytes=5,
    )

    assert controller.add_pending(_message("one")) is True
    assert controller.add_pending(_message("two")) is True
    assert controller.add_pending(_message("sixsix")) is False
    assert controller.add_pending(_message("三")) is True

    pending: list[str] = []
    while message := controller.pop_pending():
        pending.append(message.content)

    assert pending == ["三"]


@pytest.mark.asyncio
async def test_channel_manager_schedules_waiting_message_after_active_turn_finishes(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    manager = ChannelManager(framework, enabled_channels=["telegram"])
    controller = manager._controller("telegram:chat")
    controller.add_pending(_message("next"))

    done = asyncio.create_task(asyncio.sleep(0))
    controller.active_tasks.add(done)
    await done

    manager._on_task_done("telegram:chat", done)
    await asyncio.sleep(0)

    assert [message.content for message, _ in framework.process_calls] == ["next"]
    assert framework.turn_control("telegram:chat").is_cancelled is False


@pytest.mark.asyncio
async def test_channel_manager_promotes_undrained_steering_to_pending_after_turn_finishes(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    framework = FakeFramework({"telegram": FakeChannel("telegram")})
    manager = ChannelManager(framework, enabled_channels=["telegram"])
    controller = manager._controller("telegram:chat")
    controller.steering.inject(_message("late correction"))

    done = asyncio.create_task(asyncio.sleep(0))
    controller.active_tasks.add(done)
    await done

    manager._on_task_done("telegram:chat", done)
    await asyncio.sleep(0)

    assert [message.content for message, _ in framework.process_calls] == ["late correction"]
    assert framework.turn_control("telegram:chat").drain_injected() == []


def test_cli_channel_normalize_input_prefixes_shell_commands() -> None:
    channel = CliChannel.__new__(CliChannel)
    channel._mode = "shell"

    assert channel._normalize_input("ls") == ",ls"
    assert channel._normalize_input(",help") == ",help"


@pytest.mark.asyncio
async def test_cli_channel_stream_events_renders_stream_and_yields_events() -> None:
    channel = CliChannel.__new__(CliChannel)
    events: list[tuple[str, str, str]] = []
    live_handle = object()
    channel._renderer = SimpleNamespace(
        start_stream=lambda kind, text: events.append(("start", kind, text)) or live_handle,
        update_stream=lambda live, *, kind, text: events.append(("update", kind, text)),
        finish_stream=lambda live, *, kind, text: events.append(("finish", kind, text)),
        error=lambda content: events.append(("error", "error", content)),
        command_output=lambda content: events.append(("send", "command", content)),
        assistant_output=lambda content: events.append(("send", "normal", content)),
    )

    message = _message("ignored", channel="cli", kind="command", session_id="cli:1")

    async def source() -> asyncio.AsyncIterator[StreamEvent]:
        yield StreamEvent("text", {"delta": "hel"})
        yield StreamEvent("text", {"delta": "lo"})
        yield StreamEvent("final", {})

    yielded = [event async for event in channel.stream_events(message, source())]

    assert events == [
        ("start", "command", "hel"),
        ("update", "command", "hello"),
        ("finish", "command", "hello"),
    ]
    assert [event.kind for event in yielded] == ["text", "text", "final"]


def test_cli_channel_history_file_uses_workspace_hash(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"

    result = CliChannel._history_file(home, workspace)

    assert result.parent == home / "history"
    assert result.suffix == ".history"


def test_cli_renderer_stream_uses_live_with_initial_text(monkeypatch: pytest.MonkeyPatch) -> None:
    live_calls: list[tuple[str, object]] = []

    class FakeLive:
        def __init__(self, renderable, **kwargs) -> None:
            live_calls.append(("init", renderable))
            live_calls.append(("transient", kwargs["transient"]))
            self.renderable = renderable

        def start(self, *, refresh: bool = False) -> None:
            live_calls.append(("start_refresh", refresh))

        def update(self, renderable, *, refresh: bool = False) -> None:
            live_calls.append(("update_refresh", refresh))
            self.renderable = renderable

        def stop(self) -> None:
            live_calls.append(("stop", self.renderable))

    printed: list[str] = []
    console = SimpleNamespace(print=printed.append)
    monkeypatch.setattr(cli_renderer, "Live", FakeLive)

    renderer = CliRenderer(console)  # type: ignore[arg-type]
    live = renderer.start_stream("normal", "hel")
    renderer.update_stream(live, kind="normal", text="hello")  # type: ignore[arg-type]
    renderer.finish_stream(live, kind="normal", text="hello")  # type: ignore[arg-type]

    assert ("transient", False) in live_calls
    assert ("start_refresh", True) in live_calls
    assert ("update_refresh", True) in live_calls
    assert not printed


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
