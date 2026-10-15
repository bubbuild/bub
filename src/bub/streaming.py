"""Transport-neutral events produced by streaming model runs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Literal

from bub.errors import BubError
from bub.tracing import Span


@dataclass
class StreamState:
    error: BubError | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class StreamEvent:
    kind: Literal["text", "reasoning", "tool_call", "tool_result", "usage", "error", "final"]
    data: dict[str, Any]


class AsyncStreamEvents:
    def __init__(
        self,
        iterator: AsyncIterator[StreamEvent],
        *,
        state: StreamState | None = None,
        on_close: Callable[[], Awaitable[None]] | None = None,
        span: Span | None = None,
    ) -> None:
        self._iterator = iterator
        self._state = state or StreamState()
        self._on_close = on_close
        self._span = span
        self._closed = False

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self

    async def __anext__(self) -> StreamEvent:
        if self._closed:
            raise StopAsyncIteration
        try:
            # Detach before returning an event to the consumer, even across tasks.
            with self._span.activate() if self._span else nullcontext():
                return await anext(self._iterator)
        except StopAsyncIteration:
            await self._close()
            raise
        except BaseException as exc:
            if self._span:
                self._span.fail(exc)
            await self._close()
            raise

    async def aclose(self) -> None:
        """Close the source and release resources, including before first iteration."""
        if not self._closed and self._span:
            self._span.set(**{"bub.cancelled": True})
        await self._close()

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with self._span.activate() if self._span else nullcontext():
                try:
                    if close := getattr(self._iterator, "aclose", None):
                        await close()
                finally:
                    if self._on_close is not None:
                        await self._on_close()
        except BaseException as exc:
            if self._span:
                self._span.fail(exc)
            raise
        finally:
            if self._span:
                self._span.end()

    @property
    def error(self) -> BubError | None:
        return self._state.error

    @property
    def usage(self) -> dict[str, Any] | None:
        return self._state.usage
