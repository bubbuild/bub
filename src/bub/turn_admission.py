"""Turn admission primitives for channel message scheduling."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from bub.types import Envelope

TurnAdmissionAction = Literal["process", "drop", "follow_up", "steer"]


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

    session_id: str
    _queue: deque[Envelope] = field(default_factory=deque, init=False, repr=False)

    def put_nowait(self, message: Envelope) -> None:
        """Append one message."""

        self._queue.append(message)

    @property
    def count(self) -> int:
        return len(self._queue)

    def get_nowait(self) -> Envelope | None:
        """Return one queued message without waiting."""

        if not self._queue:
            return None
        return self._queue.popleft()

    def drain_nowait(self) -> list[Envelope]:
        """Drain steering input and acknowledge ownership of those messages."""

        messages = list(self._queue)
        self._queue.clear()
        return messages


@dataclass
class SessionTurnController:
    """Per-session runtime queues used by ``ChannelManager``."""

    session_id: str
    steering: SteeringBuffer
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
