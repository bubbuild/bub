import asyncio
import contextlib
import functools
from collections.abc import Collection

from loguru import logger
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from republic import StreamEvent

from bub.channels.base import Channel
from bub.channels.handler import BufferedMessageHandler
from bub.channels.message import ChannelMessage
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.types import Envelope, MessageHandler
from bub.utils import wait_until_stopped


class ChannelSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_", extra="ignore", env_file=".env")

    enabled_channels: str = Field(
        default="all", description="Comma-separated list of enabled channels, or 'all' for all channels."
    )
    debounce_seconds: float = Field(
        default=1.0,
        description="Minimum seconds between processing two messages from the same channel to prevent overload.",
    )
    max_wait_seconds: float = Field(
        default=10.0,
        description="Maximum seconds to wait for processing before new messages reach the channel.",
    )
    active_time_window: float = Field(
        default=60.0,
        description="Time window in seconds to consider a channel active for processing messages.",
    )


class ChannelManager:
    def __init__(self, framework: BubFramework, enabled_channels: Collection[str] | None = None) -> None:
        self.framework = framework
        self._channels: dict[str, Channel] = self.framework.get_channels(self.on_receive)
        self._settings = ChannelSettings()
        if enabled_channels is not None:
            self._enabled_channels = list(enabled_channels)
        else:
            self._enabled_channels = self._settings.enabled_channels.split(",")
        self._messages = asyncio.Queue[ChannelMessage]()
        self._ongoing_tasks: dict[str, set[asyncio.Task]] = {}
        self._session_handlers: dict[str, MessageHandler] = {}

    async def on_receive(self, message: ChannelMessage) -> None:
        channel = message.channel
        session_id = message.session_id
        if channel not in self._channels:
            logger.warning(f"Received message from unknown channel '{channel}', ignoring.")
            return
        if session_id not in self._session_handlers:
            handler: MessageHandler
            if self._channels[channel].needs_debounce:
                handler = BufferedMessageHandler(
                    self._messages.put,
                    active_time_window=self._settings.active_time_window,
                    max_wait_seconds=self._settings.max_wait_seconds,
                    debounce_seconds=self._settings.debounce_seconds,
                )
            else:
                handler = self._messages.put
            self._session_handlers[session_id] = handler
        await self._session_handlers[session_id](message)

    def get_channel(self, name: str) -> Channel | None:
        return self._channels.get(name)

    async def dispatch_output(self, message: Envelope) -> bool:
        channel_name = field_of(message, "output_channel", field_of(message, "channel"))
        if channel_name is None:
            return False

        channel_key = str(channel_name)
        channel = self.get_channel(channel_key)
        if channel is None:
            return False

        outbound = ChannelMessage(
            session_id=str(field_of(message, "session_id", f"{channel_key}:default")),
            channel=channel_key,
            chat_id=str(field_of(message, "chat_id", "default")),
            content=content_of(message),
            context=field_of(message, "context", {}),
            kind=field_of(message, "kind", "normal"),
        )
        await channel.send(outbound)
        return True

    async def dispatch_event(self, event: StreamEvent, message: Envelope) -> None:
        channel_name = field_of(message, "output_channel", field_of(message, "channel"))
        if channel_name is None:
            return

        channel_key = str(channel_name)
        channel = self.get_channel(channel_key)
        if channel is None:
            return

        await channel.on_event(event, message)

    async def quit(self, session_id: str) -> None:
        tasks = self._ongoing_tasks.pop(session_id, set())
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info(f"channel.manager quit session_id={session_id}, cancelled {len(tasks)} tasks")

    def enabled_channels(self) -> list[Channel]:
        if "all" in self._enabled_channels:
            # Exclude 'cli' channel from 'all' to prevent interference with other channels
            return [channel for name, channel in self._channels.items() if name != "cli" and channel.enabled]
        return [
            channel for name, channel in self._channels.items() if name in self._enabled_channels and channel.enabled
        ]

    def _on_task_done(self, session_id: str, task: asyncio.Task) -> None:
        task.exception()  # to log any exception
        tasks = self._ongoing_tasks.get(session_id, set())
        tasks.discard(task)
        if not tasks:
            self._ongoing_tasks.pop(session_id, None)

    async def listen_and_run(self) -> None:
        stop_event = asyncio.Event()
        self.framework.bind_outbound_router(self)
        for channel in self.enabled_channels():
            await channel.start(stop_event)
        logger.info("channel.manager started listening")
        try:
            while True:
                message = await wait_until_stopped(self._messages.get(), stop_event)
                task = asyncio.create_task(self.framework.process_inbound(message))
                task.add_done_callback(functools.partial(self._on_task_done, message.session_id))
                self._ongoing_tasks.setdefault(message.session_id, set()).add(task)
        except asyncio.CancelledError:
            logger.info("channel.manager received shutdown signal")
        except Exception:
            logger.exception("channel.manager error")
            raise
        finally:
            self.framework.bind_outbound_router(None)
            await self.shutdown()
            logger.info("channel.manager stopped")

    async def shutdown(self) -> None:
        count = 0
        for tasks in self._ongoing_tasks.values():
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                count += 1
        self._ongoing_tasks.clear()
        logger.info(f"channel.manager cancelled {count} in-flight tasks")
        for channel in self.enabled_channels():
            await channel.stop()
