"""Admission policy and session scheduling for channel messages."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Literal, Protocol

from bub.envelope import Envelope
from bub.turn import TurnState

type TurnAdmissionAction = Literal["process", "drop", "follow_up", "steer"]


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


class SteeringInbox(Protocol):
    """Queue boundary used when admission chooses to steer a running turn."""

    async def enqueue_message(self, message: Envelope, state: TurnState) -> None: ...
    async def drain_messages(self, state: TurnState) -> list[Envelope]: ...
    def message_count(self, state: TurnState) -> int: ...


@dataclass
class SessionTurnController:
    """Per-session queues and tasks owned by ``ChannelManager``."""

    session_id: str
    steering_inbox: SteeringInbox | None = None
    active_tasks: set[asyncio.Task] = field(default_factory=set)
    pending_queue: deque[Envelope] = field(default_factory=deque)

    def active(self) -> set[asyncio.Task]:
        return {task for task in self.active_tasks if not task.done()}

    def snapshot(self, state: TurnState) -> TurnSnapshot:
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
