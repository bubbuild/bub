"""CLI, channel work and prompt events published by the application."""

from dataclasses import dataclass
from typing import Literal

from bub.core.events import Event

type Outcome = Literal["succeeded", "failed", "cancelled"]
type PromptKind = Literal["question", "permission", "auth"]


@dataclass(frozen=True)
class CommandChanged(Event):
    """A CLI invocation started (None) or finished with an outcome."""

    outcome: Outcome | None = None


@dataclass(frozen=True)
class WorkChanged(Event):
    """A channel work snapshot, optionally including the message outcome that caused it."""

    busy: bool
    completed: Outcome | None = None


@dataclass(frozen=True)
class PromptChanged(Event):
    """A modal prompt opened (kind) or closed (None)."""

    kind: PromptKind | None
