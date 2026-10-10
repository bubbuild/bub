"""Data carried through and returned from one inbound turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bub.envelope import Envelope

if TYPE_CHECKING:
    from bub.prompt import UserContent

type TurnState = dict[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """Result of one complete message turn."""

    session_id: str
    prompt: list[UserContent]
    model_output: str
    outbounds: list[Envelope] = field(default_factory=list)
    state: TurnState = field(default_factory=dict)
