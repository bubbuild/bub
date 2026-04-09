"""Framework-neutral data aliases."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from republic import StreamEvent

type Envelope = Any
type State = dict[str, Any]
type MessageHandler = Callable[[Envelope], Coroutine[Any, Any, None]]
type OutboundDispatcher = Callable[[Envelope], Coroutine[Any, Any, bool]]


class OutboundChannelRouter(Protocol):
    async def dispatch_output(self, message: Envelope) -> bool: ...
    async def dispatch_event(self, event: StreamEvent, message: Envelope) -> None: ...
    async def quit(self, session_id: str) -> None: ...


@dataclass(frozen=True)
class TurnResult:
    """Result of one complete message turn."""

    session_id: str
    prompt: str
    model_output: str
    outbounds: list[Envelope] = field(default_factory=list)
