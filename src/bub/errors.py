"""Errors reported by Bub's model and tool execution paths."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    """Stable error kinds for execution decisions."""

    INVALID_INPUT = "invalid_input"
    CONFIG = "config"
    PROVIDER = "provider"
    TOOL = "tool"
    TEMPORARY = "temporary"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BubError(Exception):
    """Public error type for Bub execution failures."""

    kind: ErrorKind
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"[{self.kind.value}] {self.message}"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload
