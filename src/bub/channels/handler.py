import asyncio
from collections import deque
from dataclasses import dataclass

from loguru import logger

from bub.channels.message import ChannelMessage
from bub.types import MessageHandler


@dataclass
class _CommandItem:
    message: ChannelMessage


@dataclass
class _BatchItem:
    """A batch of consecutive non-command messages."""

    loop: asyncio.AbstractEventLoop
    messages: list[ChannelMessage]
    ready: asyncio.Event
    timer: asyncio.TimerHandle | None = None
    sealed: bool = False

    def schedule(self, timeout: float) -> None:
        if self.sealed:
            return
        self.ready.clear()
        if self.timer is not None:
            self.timer.cancel()
        self.timer = self.loop.call_later(timeout, self._fire)

    def _fire(self) -> None:
        self.timer = None
        self.sealed = True
        self.ready.set()


class BufferedMessageHandler:
    """Per-session message buffer with batching and strict arrival-order serialization."""

    def __init__(
        self, handler: MessageHandler, *, active_time_window: float, max_wait_seconds: float, debounce_seconds: float
    ) -> None:
        self._handler = handler
        self._loop = asyncio.get_running_loop()

        self._work: deque[_CommandItem | _BatchItem] = deque()
        self._in_processing: asyncio.Task | None = None

        self._last_active_time: float | None = None
        self.active_time_window = active_time_window
        self.max_wait_seconds = max_wait_seconds
        self.debounce_seconds = debounce_seconds

    @staticmethod
    def _is_command(message: ChannelMessage) -> bool:
        return message.content.startswith(",")

    def _ensure_worker(self) -> None:
        if self._in_processing is None:
            self._in_processing = asyncio.create_task(self._process())

    def _append_to_tail_batch(self, message: ChannelMessage) -> _BatchItem:
        if self._work and isinstance(self._work[-1], _BatchItem) and not self._work[-1].sealed:
            batch = self._work[-1]
            batch.messages.append(message)
            return batch

        batch = _BatchItem(loop=self._loop, messages=[message], ready=asyncio.Event())
        self._work.append(batch)
        return batch

    async def _process(self) -> None:
        try:
            while True:
                if not self._work:
                    return

                item = self._work[0]
                if isinstance(item, _CommandItem):
                    self._work.popleft()
                    try:
                        await self._handler(item.message)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "session.message command handler failed session_id={}, content={}",
                            item.message.session_id,
                            item.message.content,
                        )
                    continue

                await item.ready.wait()
                self._work.popleft()
                try:
                    merged = ChannelMessage.from_batch(item.messages)
                    await self._handler(merged)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    session_id = item.messages[-1].session_id if item.messages else "unknown"
                    logger.exception("session.message batch handler failed session_id={}", session_id)
        finally:
            self._in_processing = None
            if self._work:
                self._ensure_worker()

    async def __call__(self, message: ChannelMessage) -> None:
        now = self._loop.time()

        if self._is_command(message):
            logger.info(
                "session.message received command session_id={}, content={}", message.session_id, message.content
            )
            self._work.append(_CommandItem(message))
            self._ensure_worker()
            return

        if not message.is_active and (
            self._last_active_time is None or now - self._last_active_time > self.active_time_window
        ):
            self._last_active_time = None
            logger.info(
                "session.message received ignored session_id={}, content={}", message.session_id, message.content
            )
            return

        batch = self._append_to_tail_batch(message)

        if message.is_active:
            self._last_active_time = now
            logger.info(
                "session.message received active session_id={}, content={}", message.session_id, message.content
            )
            batch.schedule(self.debounce_seconds)
            self._ensure_worker()
            return

        if self._last_active_time is not None:
            logger.info("session.receive followup session_id={} message={}", message.session_id, message.content)
            batch.schedule(self.max_wait_seconds)
            self._ensure_worker()
