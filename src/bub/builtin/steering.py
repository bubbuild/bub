"""Steering inbox implementations."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable

from bub.envelope import Envelope
from bub.turn import TurnState


class InMemorySteeringInbox:
    """Process-local steering inbox keyed by runtime thread or session."""

    def __init__(self) -> None:
        self._messages: defaultdict[Hashable, deque[Envelope]] = defaultdict(deque)

    async def enqueue_message(self, message: Envelope, state: TurnState) -> None:
        self._messages[self._key(state)].append(message)

    async def drain_messages(self, state: TurnState) -> list[Envelope]:
        key = self._key(state)
        messages = list(self._messages.pop(key, ()))
        return messages

    def message_count(self, state: TurnState) -> int:
        return len(self._messages.get(self._key(state), ()))

    @staticmethod
    def _key(state: TurnState) -> Hashable:
        thread_id = state.get("_runtime_thread_id")
        if isinstance(thread_id, Hashable) and thread_id:
            return thread_id
        session_id = state.get("session_id")
        if isinstance(session_id, Hashable) and session_id:
            return session_id
        return "default"
