"""Canonical tape stores and the dynamic fork view.

There is one read data plane: physical streams. Queries are finite projections
of it; forks compose query views rather than inventing a durable overlay cursor.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, NoReturn
from uuid import uuid4

from loguru import logger

from bub.core.file_store import FileTapeStore as FileTapeStore
from bub.core.tape import TapeEntry
from bub.core.tape_log import (
    TapeOffset as TapeOffset,
)
from bub.core.tape_log import (
    TapeRecord as TapeRecord,
)
from bub.core.tape_log import (
    TapeStore as TapeStore,
)
from bub.core.tape_log import (
    decode_offset,
    encode_offset,
)
from bub.core.tape_query import (
    TapeAccess as TapeAccess,
)
from bub.core.tape_query import (
    TapeQuery as TapeQuery,
)
from bub.core.tape_query import (
    TapeQueryExecutor as TapeQueryExecutor,
)
from bub.core.tape_query import (
    TapeQuerySpec as TapeQuerySpec,
)
from bub.core.tape_query import _StreamQuery
from bub.errors import BubError, ErrorKind


@dataclass
class _MemoryLog:
    identity: str = field(default_factory=lambda: uuid4().hex)
    entries: list[TapeEntry] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class InMemoryTapeStore(_StreamQuery):
    """Event-loop-owned in-memory logs. No history copy is made on append."""

    def __init__(self) -> None:
        self._tapes: dict[str, _MemoryLog] = {}

    def _log(self, tape: str) -> _MemoryLog:
        if tape not in self._tapes:
            self._tapes[tape] = _MemoryLog()
        return self._tapes[tape]

    async def list_tapes(self) -> list[str]:
        return sorted(name for name, log in self._tapes.items() if log.entries)

    async def reset(self, tape: str) -> None:
        old = self._tapes.pop(tape, None)
        if old is not None:
            old.changed.set()

    async def append(self, tape: str, entry: TapeEntry) -> TapeRecord:
        log = self._log(tape)
        stored = TapeEntry(len(log.entries) + 1, entry.kind, dict(entry.payload), dict(entry.meta), entry.date)
        log.entries.append(stored)
        log.changed.set()
        log.changed = asyncio.Event()
        return TapeRecord(stored.copy(), encode_offset(log.identity, len(log.entries)))

    async def tail(self, tape: str) -> TapeOffset:
        log = self._log(tape)
        return encode_offset(log.identity, len(log.entries))

    @asynccontextmanager
    async def stream(
        self, tape: str, *, start: TapeOffset | None = None, stop: TapeOffset | None = None
    ) -> AsyncIterator[AsyncIterator[TapeRecord]]:
        log = self._log(tape)
        position = decode_offset(start, log.identity, len(log.entries)) if start is not None else 0
        end = decode_offset(stop, log.identity, len(log.entries)) if stop is not None else None
        if end is not None and position > end:
            raise BubError(ErrorKind.INVALID_INPUT, "Tape start is after stop.")

        async def read() -> AsyncGenerator[TapeRecord, None]:
            cursor = position
            while end is None or cursor < end:
                if cursor < len(log.entries):
                    entry = log.entries[cursor].copy()
                    cursor += 1
                    yield TapeRecord(entry, encode_offset(log.identity, cursor))
                else:
                    if self._tapes.get(tape) is not log:
                        raise BubError(ErrorKind.INVALID_INPUT, "Tape was reset while following.")
                    await log.changed.wait()

        async with aclosing(read()) as records:
            yield records


class UnavailableTapeStore(_StreamQuery):
    """Unavailable canonical store; failure is deferred until an operation."""

    def __init__(self, message: str) -> None:
        self._message = message

    def _raise(self) -> NoReturn:
        raise BubError(ErrorKind.INVALID_INPUT, self._message)

    async def list_tapes(self) -> list[str]:
        self._raise()

    async def reset(self, tape: str) -> None:
        self._raise()

    async def append(self, tape: str, entry: TapeEntry) -> TapeRecord:
        self._raise()

    async def tail(self, tape: str) -> TapeOffset:
        self._raise()

    @asynccontextmanager
    async def stream(
        self, tape: str, *, start: TapeOffset | None = None, stop: TapeOffset | None = None
    ) -> AsyncIterator[AsyncIterator[TapeRecord]]:
        self._raise()
        yield


class ForkTapeStore:
    """Current parent query + local writes, not an append-only physical log.

    Keep the established local anchor/kinds/limit overlay semantics, including
    the parent's own query policy. Unmanaged names are delegated immediately.
    """

    def __init__(self, parent: TapeAccess, tape: str, *, sidecars: Iterable[str] = ()) -> None:
        self._parent = parent
        self._store = InMemoryTapeStore()
        self._tape = tape
        self._sidecars = tuple(dict.fromkeys(sidecars))
        self._managed_tapes = {tape, *self._sidecars}
        self._reset_tapes: set[str] = set()

    async def list_tapes(self) -> list[str]:
        return await self._parent.list_tapes()

    async def reset(self, tape: str) -> None:
        if tape not in self._managed_tapes:
            await self._parent.reset(tape)
            return
        await self._store.reset(tape)
        self._reset_tapes.add(tape)

    @asynccontextmanager
    async def select(self, tape: str, spec: TapeQuerySpec) -> AsyncIterator[AsyncIterator[TapeEntry]]:
        async with AsyncExitStack() as stack:
            if tape not in self._managed_tapes:
                yield await stack.enter_async_context(self._parent.select(tape, spec))
                return
            parent = None
            if tape not in self._reset_tapes:
                try:
                    parent = await stack.enter_async_context(self._parent.select(tape, spec))
                except BubError as exc:
                    if exc.kind is not ErrorKind.NOT_FOUND:
                        raise
            local: list[TapeEntry] = []
            cut = await self._store.tail(tape)
            async with self._store.stream(tape, stop=cut) as records:
                async for record in records:
                    entry = record.entry
                    if entry.kind == "anchor" and (
                        spec.after_last or (spec.after_anchor and entry.payload.get("name") == spec.after_anchor)
                    ):
                        local.clear()
                        parent = None
                        continue
                    if not spec.kinds or entry.kind in spec.kinds:
                        local.append(entry)
            if spec.limit is not None and spec.limit < 0:
                raise ValueError("Fork query limits must be nonnegative.")
            async with aclosing(_overlay(parent, local, spec.limit)) as entries:
                yield entries

    @staticmethod
    def _redact_prompt(prompt: list[dict]) -> Any:
        if not isinstance(prompt, list):
            return prompt
        return [part for part in prompt if part.get("type") == "text"]

    @staticmethod
    def _redact_payload(payload: dict) -> None:
        if "content" in payload:
            payload["content"] = ForkTapeStore._redact_prompt(payload["content"])
        elif "prompt" in payload:
            payload["prompt"] = ForkTapeStore._redact_prompt(payload["prompt"])

    async def append(self, tape: str, entry: TapeEntry) -> TapeRecord:
        self._redact_payload(entry.payload)
        target = self._store if tape in self._managed_tapes else self._parent
        return await target.append(tape, entry)

    async def _merge(self, tape: str) -> int:
        if tape in self._reset_tapes:
            await self._parent.reset(tape)
        count = 0
        cut = await self._store.tail(tape)
        async with self._store.stream(tape, stop=cut) as records:
            async for record in records:
                await self._parent.append(tape, record.entry)
                count += 1
        return count

    async def merge_back(self) -> None:
        total = 0
        for sidecar in self._sidecars:
            try:
                total += await self._merge(sidecar)
            except Exception as exc:
                logger.warning('Failed to merge sidecar "{}" into tape "{}": {}', sidecar, self._tape, exc)
                await self._store.append(
                    self._tape,
                    TapeEntry.event(
                        "sidecar.merge", {"tape": sidecar, "status": "error", "error": str(exc)}, context=False
                    ),
                )
        total += await self._merge(self._tape)
        if total:
            logger.info('Merged {} entries into tape fork "{}"', total, self._tape)


async def _overlay(
    parent: AsyncIterator[TapeEntry] | None, local: list[TapeEntry], limit: int | None
) -> AsyncGenerator[TapeEntry, None]:
    count = 0
    if limit == 0:
        return
    if parent is not None:
        try:
            async for entry in parent:
                yield entry
                count += 1
                if limit is not None and count >= limit:
                    return
        except BubError as exc:
            # Missing parent anchors still allow a local-only fork view.
            if exc.kind is not ErrorKind.NOT_FOUND:
                raise
    for entry in local:
        yield entry
        count += 1
        if limit is not None and count >= limit:
            return
