"""Finite query views over physical streams, separate from log cursors."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from typing import Protocol, Self

from bub.core.tape import TapeEntry
from bub.core.tape_log import TapeRecord, TapeStore
from bub.errors import BubError, ErrorKind


@dataclass(frozen=True, slots=True)
class TapeQuerySpec:
    """Query values, independent of their execution binding."""

    text: str | None = None
    after_anchor: str | None = None
    after_last: bool = False
    between_anchors: tuple[str, str] | None = None
    between_dates: tuple[str, str] | None = None
    kinds: tuple[str, ...] = ()
    limit: int | None = None


class TapeQueryExecutor(Protocol):
    """Open a finite query view.

    Canonical execution fixes one source cut. Fork execution prepares a parent
    query and a local cut for this invocation, not for the fork's lifetime.
    Search ordering is not necessarily commit ordering. Result iteration has
    no durable resume offset, follow mode, or acknowledgement semantics.
    """

    def select(self, tape: str, spec: TapeQuerySpec) -> AbstractAsyncContextManager[AsyncIterator[TapeEntry]]: ...


@dataclass(frozen=True, slots=True)
class TapeQuery:
    """Keep the fluent query facade; replace only the execution mechanism."""

    tape: str
    store: TapeQueryExecutor
    spec: TapeQuerySpec = field(default_factory=TapeQuerySpec)

    def query(self, value: str) -> Self:
        return replace(self, spec=replace(self.spec, text=value))

    def after_anchor(self, name: str) -> Self:
        return replace(self, spec=replace(self.spec, after_anchor=name or None, after_last=False))

    def last_anchor(self) -> Self:
        return replace(self, spec=replace(self.spec, after_anchor=None, after_last=True))

    def between_anchors(self, start: str, end: str) -> Self:
        return replace(self, spec=replace(self.spec, between_anchors=(start, end)))

    def between_dates(self, start: str | date, end: str | date) -> Self:
        start_value = start.isoformat() if isinstance(start, date) else start
        end_value = end.isoformat() if isinstance(end, date) else end
        return replace(self, spec=replace(self.spec, between_dates=(start_value, end_value)))

    def kinds(self, *kinds: str) -> Self:
        return replace(self, spec=replace(self.spec, kinds=kinds))

    def limit(self, value: int) -> Self:
        return replace(self, spec=replace(self.spec, limit=value))

    def stream(self) -> AbstractAsyncContextManager[AsyncIterator[TapeEntry]]:
        """Finite result streaming, not raw-log replay or follow."""
        return self.store.select(self.tape, self.spec)

    async def all(self) -> list[TapeEntry]:
        """Collect one finite result and close its read resources."""
        async with self.stream() as entries:
            return [entry async for entry in entries]


class TapeAccess(TapeQueryExecutor, Protocol):
    """Read/write view operations used by Tape, including fork overlays.

    A canonical backend may implement both this protocol and the physical
    log protocol. ForkTapeStore implements this view protocol by composition;
    it must not pretend that its dynamic overlay has one append-only cursor.
    """

    async def append(self, tape: str, entry: TapeEntry) -> TapeRecord:
        """Return the physical write target's receipt, not an overlay cursor."""
        ...

    async def reset(self, tape: str) -> None: ...

    async def list_tapes(self) -> list[str]: ...


def _parse_datetime_boundary(value: str, *, is_end: bool) -> datetime:
    if "T" not in value and " " not in value:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            boundary_time = time.max if is_end else time.min
            return datetime.combine(parsed_date, boundary_time, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError as exc:
            raise BubError(ErrorKind.INVALID_INPUT, f"Invalid ISO date or datetime: '{value}'.") from exc
        boundary_time = time.max if is_end else time.min
        parsed = datetime.combine(parsed_date, boundary_time, tzinfo=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entry_in_datetime_range(entry: TapeEntry, start_dt: datetime, end_dt: datetime) -> bool:
    entry_dt = _parse_datetime_boundary(entry.date, is_end=False)
    return start_dt <= entry_dt <= end_dt


def _entry_matches_query(entry: TapeEntry, query: str) -> bool:
    needle = query.casefold()
    haystack = json.dumps(
        {
            "kind": entry.kind,
            "date": entry.date,
            "payload": entry.payload,
            "meta": entry.meta,
        },
        sort_keys=True,
        default=str,
    ).casefold()
    return needle in haystack


def _query_dates(spec: TapeQuerySpec) -> tuple[datetime, datetime] | None:
    if spec.between_dates is None:
        return None
    left, right = spec.between_dates
    dates = (_parse_datetime_boundary(left, is_end=False), _parse_datetime_boundary(right, is_end=True))
    if dates[0] > dates[1]:
        raise BubError(ErrorKind.INVALID_INPUT, "Start date must be earlier than or equal to end date.")
    return dates


class _StreamQuery(TapeStore):
    """Compute finite query views in one pass through the physical stream."""

    @asynccontextmanager
    async def select(self, tape: str, spec: TapeQuerySpec) -> AsyncIterator[AsyncIterator[TapeEntry]]:
        dates = _query_dates(spec)
        cut = await self.tail(tape)
        async with (
            self.stream(tape, stop=cut) as records,
            aclosing(_select_range(records, spec)) as selected,
            aclosing(_filter_entries(selected, spec, dates)) as entries,
        ):
            yield entries


async def _select_range(records: AsyncIterator[TapeRecord], spec: TapeQuerySpec) -> AsyncGenerator[TapeEntry, None]:
    anchored = spec.between_anchors is not None or spec.after_last or spec.after_anchor is not None
    # A later start supersedes the entire candidate range. Nothing can be emitted
    # until EOF, even after a matching end. Buffer this range, not a second log.
    start = spec.between_anchors[0] if spec.between_anchors else spec.after_anchor
    last = spec.after_last and spec.between_anchors is None
    found, complete = not anchored, False
    pending: list[TapeEntry] = []
    async for record in records:
        entry = record.entry
        if not anchored:
            yield entry
            continue
        if entry.kind == "anchor":
            name = entry.payload.get("name")
            if last or name == start:
                pending.clear()
                found, complete = True, False
                continue
            if found and spec.between_anchors and name == spec.between_anchors[1]:
                complete = True
        if found and not complete:
            pending.append(entry)

    if not found:
        message = "No anchors found in tape." if last else f"Anchor '{start}' was not found."
        raise BubError(ErrorKind.NOT_FOUND, message)
    if spec.between_anchors and not complete:
        raise BubError(ErrorKind.NOT_FOUND, f"Anchor '{spec.between_anchors[1]}' was not found.")
    for entry in pending:
        yield entry


async def _filter_entries(
    entries: AsyncIterator[TapeEntry], spec: TapeQuerySpec, dates: tuple[datetime, datetime] | None
) -> AsyncGenerator[TapeEntry, None]:
    if spec.limit == 0:
        return
    # Negative limits retain Python slicing semantics without collecting history.
    pending: deque[TapeEntry] = deque()
    count = 0
    async for entry in entries:
        if dates is not None and not _entry_in_datetime_range(entry, *dates):
            continue
        if spec.text and not _entry_matches_query(entry, spec.text):
            continue
        if spec.kinds and entry.kind not in spec.kinds:
            continue
        if spec.limit is not None and spec.limit < 0:
            pending.append(entry)
            if len(pending) > -spec.limit:
                yield pending.popleft()
        else:
            count += 1
            yield entry
            if spec.limit is not None and count >= spec.limit:
                return
