"""Framework-neutral data aliases."""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from bub.runtime import StreamEvent

if TYPE_CHECKING:
    from bub.turn_admission import AdmitDecision, TurnSnapshot

type Envelope = Any
type State = dict[str, Any]
type MessageHandler = Callable[[Envelope], Coroutine[Any, Any, None]]
type OutboundDispatcher = Callable[[Envelope], Coroutine[Any, Any, bool]]


class OutboundChannelRouter(Protocol):
    async def dispatch_output(self, message: Envelope) -> bool: ...
    def wrap_stream(self, message: Envelope, stream: AsyncIterable[StreamEvent]) -> AsyncIterable[StreamEvent]: ...
    async def quit(self, session_id: str) -> None: ...
    async def admit_channel_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None: ...


@dataclass(frozen=True)
class TurnResult:
    """Result of one complete message turn."""

    session_id: str
    prompt: str | list[dict[str, Any]]
    model_output: str
    outbounds: list[Envelope] = field(default_factory=list)
    state: State = field(default_factory=dict)


class SteeringInboxProtocol(Protocol):
    async def enqueue_message(self, message: Envelope, state: State) -> None: ...
    async def drain_messages(self, state: State) -> list[Envelope]: ...
    def message_count(self, state: State) -> int: ...
