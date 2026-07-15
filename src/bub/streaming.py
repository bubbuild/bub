"""Transport-neutral events produced by streaming model runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from bub.errors import BubError


@dataclass
class StreamState:
    error: BubError | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class StreamEvent:
    kind: Literal["text", "reasoning", "tool_call", "tool_result", "usage", "error", "final"]
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
