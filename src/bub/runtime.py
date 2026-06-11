"""Small runtime primitives shared by Bub core and channels."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class ErrorKind(StrEnum):
    """Stable error kinds for runtime decisions."""

    INVALID_INPUT = "invalid_input"
    CONFIG = "config"
    PROVIDER = "provider"
    TOOL = "tool"
    TEMPORARY = "temporary"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BubError(Exception):
    """Public error type for Bub runtime failures."""

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


@dataclass
class StreamState:
    error: BubError | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class StreamEvent:
    kind: Literal["text", "tool_call", "tool_result", "usage", "error", "final"]
    data: dict[str, Any]


class AsyncStreamEvents:
    def __init__(self, iterator: AsyncIterator[StreamEvent], *, state: StreamState | None = None) -> None:
        self._iterator = iterator
        self._state = state or StreamState()

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._iterator

    @property
    def error(self) -> BubError | None:
        return self._state.error

    @property
    def usage(self) -> dict[str, Any] | None:
        return self._state.usage
