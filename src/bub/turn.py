"""Data carried through and returned from one inbound turn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bub.envelope import Envelope

type TurnState = dict[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """Result of one complete message turn."""

    session_id: str
    prompt: str | list[dict[str, Any]]
    model_output: str
    outbounds: list[Envelope] = field(default_factory=list)
    state: TurnState = field(default_factory=dict)
