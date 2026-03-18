import asyncio

from loguru import logger

from bub.channels.message import ChannelMessage
from bub.types import MessageHandler


class BufferedMessageHandler:
    """A message handler that buffers incoming messages and processes them in batch with debounce and active time window."""

    def __init__(
        self, handler: MessageHandler, *, active_time_window: float, max_wait_seconds: float, debounce_seconds: float
    ) -> None:
        self._handler = handler
        self._pending_messages: list[ChannelMessage] = []
        self._last_active_time: float | None = None
        self._event = asyncio.Event()
        self._timer: asyncio.TimerHandle | None = None
        self._in_processing: asyncio.Task | None = None
        self._loop = asyncio.get_running_loop()

        self.active_time_window = active_time_window
        self.max_wait_seconds = max_wait_seconds
        self.debounce_seconds = debounce_seconds

    def _reset_timer(self, timeout: float) -> None:
        self._event.clear()
        if self._timer:
            self._timer.cancel()
        self._timer = self._loop.call_later(timeout, self._event.set)

    async def _process(self) -> None:
        await self._event.wait()
        message = ChannelMessage.from_batch(self._pending_messages)
        self._pending_messages.clear()
        self._in_processing = None
        await self._handler(message)

    async def __call__(self, message: ChannelMessage) -> None:
        now = self._loop.time()
        if message.content.startswith(","):
            logger.info(
                "session.message received command session_id={}, content={}", message.session_id, message.content
            )
            await self._handler(message)
            return
        if not message.is_active and (
            self._last_active_time is None or now - self._last_active_time > self.active_time_window
        ):
            self._last_active_time = None
            logger.info(
                "session.message received ignored session_id={}, content={}", message.session_id, message.content
            )
            return
        self._pending_messages.append(message)
        if message.is_active:
            self._last_active_time = now
            logger.info(
                "session.message received active session_id={}, content={}", message.session_id, message.content
            )
            self._reset_timer(self.debounce_seconds)
            if self._in_processing is None:
                self._in_processing = asyncio.create_task(self._process())
        elif self._last_active_time is not None and self._in_processing is None:
            logger.info("session.receive followup session_id={} message={}", message.session_id, message.content)
            self._reset_timer(self.max_wait_seconds)
            self._in_processing = asyncio.create_task(self._process())
