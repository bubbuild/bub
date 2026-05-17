"""Turn admission primitives for channel message scheduling."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from bub.types import Envelope


class AdmitAction(StrEnum):
    """Actions an ``admit_message`` hook can return."""

    PROCESS = "process"
    DROP = "drop"
    WAIT = "wait"
    STEER = "steer"


TurnAdmissionAction = AdmitAction | Literal["process", "drop", "wait", "steer"]


@dataclass(frozen=True)
class AdmitDecision:
    """Decision returned by ``admit_message`` hooks."""

    action: TurnAdmissionAction
    reason: str | None = None


@dataclass(frozen=True)
class TurnSnapshot:
    """Snapshot of current session turn state exposed to admission hooks."""

    session_id: str
    is_running: bool
    running_count: int
    pending_count: int
    steering_count: int


@dataclass
class SteeringBuffer:
    """Per-session queue for steering messages offered to active turns."""

    _queue: asyncio.Queue[Envelope] = field(default_factory=asyncio.Queue, init=False, repr=False)

    def put_nowait(self, message: Envelope) -> bool:
        """Append one message."""

        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True

    @property
    def count(self) -> int:
        return self._queue.qsize()

    def has_messages(self) -> bool:
        return not self._queue.empty()

    def get_nowait(self) -> Envelope | None:
        """Return one queued message without waiting."""

        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def drain_nowait(self) -> list[Envelope]:
        """Return all queued messages without waiting."""

        messages: list[Envelope] = []
        while True:
            message = self.get_nowait()
            if message is None:
                return messages
            messages.append(message)


@dataclass
class SteeringHandle:
    """Control surface exposed to model hooks through turn state."""

    session_id: str
    buffer: SteeringBuffer

    def put_nowait(self, message: Envelope) -> bool:
        return self.buffer.put_nowait(message)

    @property
    def count(self) -> int:
        return self.buffer.count

    def has_messages(self) -> bool:
        return self.buffer.has_messages()

    def get_nowait(self) -> Envelope | None:
        """Drain one steering input and acknowledge ownership of it."""

        return self.buffer.get_nowait()

    def drain_nowait(self) -> list[Envelope]:
        """Drain steering input and acknowledge ownership of those messages."""

        return self.buffer.drain_nowait()


@dataclass
class SessionTurnController:
    """Per-session runtime queues used by ``ChannelManager``."""

    session_id: str
    steering: SteeringHandle
    active_tasks: set[asyncio.Task] = field(default_factory=set)
    pending_queue: deque[Envelope] = field(default_factory=deque)

    def active(self) -> set[asyncio.Task]:
        return {task for task in self.active_tasks if not task.done()}

    def snapshot(self) -> TurnSnapshot:
        running_count = len(self.active())
        return TurnSnapshot(
            session_id=self.session_id,
            is_running=running_count > 0,
            running_count=running_count,
            pending_count=len(self.pending_queue),
            steering_count=self.steering.count,
        )

    def add_pending(self, message: Envelope) -> bool:
        self.pending_queue.append(message)
        return True

    def add_pending_left(self, message: Envelope) -> bool:
        self.pending_queue.appendleft(message)
        return True

    def pop_pending(self) -> Envelope | None:
        if not self.pending_queue:
            return None
        return self.pending_queue.popleft()

    def clear_pending(self) -> None:
        self.pending_queue.clear()

    def promote_steering_to_pending(self) -> None:
        for message in reversed(self.steering.drain_nowait()):
            self.add_pending_left(message)
