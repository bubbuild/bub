"""Turn admission primitives for channel message scheduling."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from bub.types import Envelope, State, SteeringInboxProtocol

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
    steering_count: int = 0


@dataclass
class SessionTurnController:
    """Per-session runtime queues used by ``ChannelManager``."""

    session_id: str
    steering_inbox: SteeringInboxProtocol | None = None
    active_tasks: set[asyncio.Task] = field(default_factory=set)
    pending_queue: deque[Envelope] = field(default_factory=deque)

    def active(self) -> set[asyncio.Task]:
        return {task for task in self.active_tasks if not task.done()}

    def snapshot(self, state: State) -> TurnSnapshot:
        running_count = len(self.active())
        return TurnSnapshot(
            session_id=self.session_id,
            is_running=running_count > 0,
            running_count=running_count,
            pending_count=len(self.pending_queue),
            steering_count=self.steering_inbox.message_count(state) if self.steering_inbox else 0,
        )

    def add_pending(self, message: Envelope) -> bool:
        self.pending_queue.append(message)
        return True

    def pop_pending(self) -> Envelope | None:
        if not self.pending_queue:
            return None
        return self.pending_queue.popleft()

    def clear_pending(self) -> None:
        self.pending_queue.clear()
