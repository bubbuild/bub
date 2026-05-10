"""Turn admission primitives for channel message scheduling."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from bub.envelope import content_of
from bub.types import Envelope


class AdmitAction(StrEnum):
    """Actions an ``admit_message`` hook can return."""

    PROCESS = "process"
    INJECT = "inject"
    WAIT = "wait"
    DROP = "drop"
    CANCEL_AND_PROCESS = "cancel_and_process"


TurnAdmissionAction = AdmitAction | Literal["process", "wait", "drop", "cancel_and_process", "inject"]


@dataclass(frozen=True)
class AdmitDecision:
    """Decision returned by ``admit_message`` hooks."""

    action: TurnAdmissionAction
    reason: str | None = None
    fallback: AdmitAction | None = None


@dataclass(frozen=True)
class TurnSnapshot:
    """Snapshot of current session turn state exposed to admission hooks."""

    session_id: str
    is_running: bool
    running_count: int
    pending_count: int
    steering_count: int
    supports_steering: bool


@dataclass
class SteeringBuffer:
    """Bounded per-session queue for steering messages injected into active turns."""

    max_size: int = 32
    max_bytes: int = 65536
    _queue: asyncio.Queue[Envelope] = field(init=False, repr=False)
    _bytes: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_size < 1:
            raise ValueError("max_size must be at least 1")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self._queue = asyncio.Queue(maxsize=self.max_size)

    def put_nowait(self, message: Envelope) -> bool:
        """Append one message, dropping the oldest entry if the queue is full."""

        size = _message_size(message)
        if size > self.max_bytes:
            return False

        while self._queue.full() or self._bytes + size > self.max_bytes:
            try:
                dropped = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._bytes = max(0, self._bytes - _message_size(dropped))
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            return False
        self._bytes += size
        return True

    @property
    def count(self) -> int:
        return self._queue.qsize()

    @property
    def bytes(self) -> int:
        return self._bytes

    def has_messages(self) -> bool:
        return not self._queue.empty()

    def drain_nowait(self) -> list[Envelope]:
        """Return all queued messages without waiting."""

        messages: list[Envelope] = []
        while True:
            try:
                message = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return messages
            self._bytes = max(0, self._bytes - _message_size(message))
            messages.append(message)

    def drain_one_nowait(self) -> list[Envelope]:
        try:
            message = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return []
        self._bytes = max(0, self._bytes - _message_size(message))
        return [message]

    def drain_latest_nowait(self) -> list[Envelope]:
        messages = self.drain_nowait()
        if not messages:
            return []
        return [messages[-1]]


class DrainMode(StrEnum):
    ALL = "all"
    ONE = "one"
    LATEST = "latest"


@dataclass
class TurnControl:
    """Control surface exposed to a running turn through state."""

    session_id: str
    buffer: SteeringBuffer
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def reset_cancel(self) -> None:
        self.cancel_event.clear()

    def inject(self, message: Envelope) -> bool:
        return self.buffer.put_nowait(message)

    @property
    def count(self) -> int:
        return self.buffer.count

    def has_messages(self) -> bool:
        return self.buffer.has_messages()

    async def drain(self, *, mode: DrainMode = DrainMode.ALL) -> list[Envelope]:
        if mode == DrainMode.ONE:
            return self.buffer.drain_one_nowait()
        if mode == DrainMode.LATEST:
            return self.buffer.drain_latest_nowait()
        return self.buffer.drain_nowait()

    def drain_injected(self) -> list[Envelope]:
        return self.buffer.drain_nowait()


@dataclass
class SessionTurnController:
    """Per-session runtime queues used by ``ChannelManager``."""

    session_id: str
    steering: TurnControl
    active_tasks: set[asyncio.Task] = field(default_factory=set)
    pending_queue: deque[Envelope] = field(default_factory=deque)
    max_pending: int = 32
    max_pending_bytes: int = 65536
    _pending_bytes: int = field(default=0, init=False, repr=False)

    def active(self) -> set[asyncio.Task]:
        return {task for task in self.active_tasks if not task.done()}

    def snapshot(self, *, supports_steering: bool) -> TurnSnapshot:
        running_count = len(self.active())
        return TurnSnapshot(
            session_id=self.session_id,
            is_running=running_count > 0,
            running_count=running_count,
            pending_count=len(self.pending_queue),
            steering_count=self.steering.count,
            supports_steering=supports_steering,
        )

    def add_pending(self, message: Envelope) -> bool:
        size = _message_size(message)
        if size > self.max_pending_bytes:
            return False
        while len(self.pending_queue) >= self.max_pending or self._pending_bytes + size > self.max_pending_bytes:
            dropped = self.pending_queue.popleft()
            self._pending_bytes = max(0, self._pending_bytes - _message_size(dropped))
        self.pending_queue.append(message)
        self._pending_bytes += size
        return True

    def pop_pending(self) -> Envelope | None:
        if not self.pending_queue:
            return None
        message = self.pending_queue.popleft()
        self._pending_bytes = max(0, self._pending_bytes - _message_size(message))
        return message


def _message_size(message: Envelope) -> int:
    return len(content_of(message).encode("utf-8"))
