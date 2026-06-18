"""Protocol-neutral runtime option types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeChoice:
    """One selectable runtime value."""

    id: str
    name: str | None = None
    description: str | None = None
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class RuntimeOptions:
    """Runtime choices that a channel or adapter may present to a user."""

    models: list[RuntimeChoice] = field(default_factory=list)
    current_model: str | None = None
