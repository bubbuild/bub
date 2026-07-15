"""Model choices that channels and adapters may present to users."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelChoice:
    """One selectable model."""

    id: str
    name: str | None = None
    description: str | None = None
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class ModelOptions:
    """Model choices and the current selection for one session."""

    models: list[ModelChoice] = field(default_factory=list)
    current_model: str | None = None
