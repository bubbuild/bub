"""Call boundaries between channels and the Bub framework."""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Protocol

from bub.envelope import Envelope
from bub.streaming import StreamEvent

if TYPE_CHECKING:
    from bub.channels.admission import AdmitDecision, TurnSnapshot

type MessageHandler = Callable[[Envelope], Coroutine[Any, Any, None]]


class ChannelRouter(Protocol):
    """Outbound and admission operations supplied by a channel runtime."""

    async def dispatch_output(self, message: Envelope) -> bool: ...
    def wrap_stream(self, message: Envelope, stream: AsyncIterable[StreamEvent]) -> AsyncIterable[StreamEvent]: ...
    async def quit(self, session_id: str) -> None: ...
    async def admit_channel_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None: ...
