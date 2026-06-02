from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from republic import StreamEvent

from bub.channels.base import Channel, Interface, Lifecycle
from bub.channels.cli import CliChannel
from bub.channels.cli.renderer import CliRenderer
from bub.channels.handler import BufferedMessageHandler
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.channels.telegram import BubMessageFilter, TelegramChannel, TelegramMessageParser


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
    manager._ongoing_tasks["telegram:chat"] = {task}

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
    manager._ongoing_tasks["session:target"] = {target_task}
    manager._ongoing_tasks["session:other"] = {other_task}

    await manager.quit("session:target")

    assert target_task.cancelled()
    assert "session:target" not in manager._ongoing_tasks
    assert other_task.cancelled() is False
    assert manager._ongoing_tasks["session:other"] == {other_task}

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
    manager._ongoing_tasks["session:target"] = {current_task, target_task}

    await manager.quit("session:target")

    assert current_task.cancelled() is False
    assert target_task.cancelled()
    assert "session:target" not in manager._ongoing_tasks


@pytest.mark.asyncio
async def test_channel_manager_done_callback_handles_cancelled_task(load_config) -> None:
    _load_channel_config(load_config, enabled_channels="telegram")
    manager = ChannelManager(FakeFramework({"telegram": FakeChannel("telegram")}), enabled_channels=["telegram"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(never_finish())
    manager._ongoing_tasks["session:target"] = {task}
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    manager._on_task_done("session:target", task)

    assert "session:target" not in manager._ongoing_tasks


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
    assert printed == [("hel", "", False), ("lo", "", False), ("\n", None, None)]
    assert [event.kind for event in yielded] == ["text", "text", "final"]


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
    printed: list[str] = []
    renderer = CliRenderer(SimpleNamespace(print=printed.append))  # type: ignore[arg-type]

    renderer.print_head(kind)  # type: ignore[arg-type]

    assert printed == [expected]


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
