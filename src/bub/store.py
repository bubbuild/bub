"""Async, streaming persistence contracts for Bub tapes."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time
from datetime import date as date_type
from pathlib import Path
from typing import Protocol, Self

from cloudevents.core.v1.event import CloudEvent
from loguru import logger

from bub.errors import BubError, ErrorKind
from bub.tape import TapeRecord, bub_event, bub_event_type, event_data, event_payload
from bub.tape_codec import TapeCodec, TapeCodecError
from bub.utils import get_entry_text, iterate_in_thread


class TapeStore(Protocol):
    """Append-only, asynchronous storage for semantic tape entries."""

    async def list_tapes(self) -> list[str]: ...

    async def reset(self, tape: str) -> None: ...

    def scan(self, query: TapeQuery) -> AsyncIterator[TapeRecord]: ...

    async def append(self, tape: str, event: CloudEvent) -> TapeRecord: ...


@dataclass(frozen=True)
class TapeQuery:
    """A store-independent description of a finite tape scan."""

    tape: str
    _query: str | None = None
    _after_anchor: str | None = None
    _after_last: bool = False
    _between_anchors: tuple[str, str] | None = None
    _between_dates: tuple[str, str] | None = None
    _types: tuple[str, ...] = field(default_factory=tuple)
    _after_cursor: int | None = None
    _limit: int | None = None

    def query(self, value: str) -> Self:
        return replace(self, _query=value)

    def after_anchor(self, name: str) -> Self:
        if not name:
            return replace(self, _after_anchor=None, _after_last=False)
        return replace(self, _after_anchor=name, _after_last=False, _between_anchors=None)

    def last_anchor(self) -> Self:
        return replace(self, _after_anchor=None, _after_last=True, _between_anchors=None)

    def between_anchors(self, start: str, end: str) -> Self:
        return replace(
            self,
            _after_anchor=None,
            _after_last=False,
            _between_anchors=(start, end),
        )

    def between_dates(self, start: str | date_type, end: str | date_type) -> Self:
        start_value = start.isoformat() if isinstance(start, date_type) else start
        end_value = end.isoformat() if isinstance(end, date_type) else end
        return replace(self, _between_dates=(start_value, end_value))

    def types(self, *event_types: str) -> Self:
        return replace(self, _types=event_types)

    def after(self, cursor: int) -> Self:
        if cursor < 0:
            raise BubError(ErrorKind.INVALID_INPUT, "Tape cursor must be non-negative.")
        return replace(self, _after_cursor=cursor)

    def limit(self, value: int) -> Self:
        if value < 1:
            raise BubError(ErrorKind.INVALID_INPUT, "Tape query limit must be positive.")
        return replace(self, _limit=value)


def _parse_datetime_boundary(value: str, *, is_end: bool) -> datetime:
    if "T" not in value and " " not in value:
        try:
            parsed_date = date_type.fromisoformat(value)
        except ValueError:
            pass
        else:
            boundary_time = time.max if is_end else time.min
            return datetime.combine(parsed_date, boundary_time, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed_date = date_type.fromisoformat(value)
        except ValueError as exc:
            raise BubError(ErrorKind.INVALID_INPUT, f"Invalid ISO date or datetime: '{value}'.") from exc
        boundary_time = time.max if is_end else time.min
        parsed = datetime.combine(parsed_date, boundary_time, tzinfo=UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _record_in_datetime_range(record: TapeRecord, start: datetime, end: datetime) -> bool:
    entry_date = record.event.get_time()
    if entry_date is None:
        return False
    return start <= entry_date <= end


def _record_matches_query(record: TapeRecord, query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    return needle in get_entry_text(record).casefold()


async def _between_anchor_entries(
    records: AsyncIterable[TapeRecord], start_name: str, end_name: str
) -> AsyncIterator[TapeRecord]:
    found_start = False
    found_end = False
    buffered: list[TapeRecord] = []
    anchor_type = bub_event_type("context.anchor")
    async for record in records:
        if record.event.get_type() == anchor_type and event_payload(record.event).get("name") == start_name:
            found_start = True
            found_end = False
            buffered.clear()
            continue
        if not found_start:
            continue
        if record.event.get_type() == anchor_type and event_payload(record.event).get("name") == end_name:
            found_end = True
            continue
        if not found_end:
            buffered.append(record)
    if not found_start:
        raise BubError(ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found.")
    if not found_end:
        raise BubError(ErrorKind.NOT_FOUND, f"Anchor '{end_name}' was not found after '{start_name}'.")
    for record in buffered:
        yield record


async def _after_anchor_entries(
    records: AsyncIterable[TapeRecord], anchor_name: str | None
) -> AsyncIterator[TapeRecord]:
    found = False
    buffered: list[TapeRecord] = []
    anchor_type = bub_event_type("context.anchor")
    async for record in records:
        matches = record.event.get_type() == anchor_type and (
            anchor_name is None or event_payload(record.event).get("name") == anchor_name
        )
        if matches:
            found = True
            buffered.clear()
            continue
        if found:
            buffered.append(record)
    if not found:
        message = "No anchors found in tape." if anchor_name is None else f"Anchor '{anchor_name}' was not found."
        raise BubError(ErrorKind.NOT_FOUND, message)
    for record in buffered:
        yield record


def _window_entries(records: AsyncIterable[TapeRecord], query: TapeQuery) -> AsyncIterator[TapeRecord]:
    if query._between_anchors is not None:
        return _between_anchor_entries(records, *query._between_anchors)
    if query._after_last:
        return _after_anchor_entries(records, None)
    if query._after_anchor is not None:
        return _after_anchor_entries(records, query._after_anchor)
    return records.__aiter__()


async def _scan_query(records: AsyncIterable[TapeRecord], query: TapeQuery) -> AsyncIterator[TapeRecord]:
    date_range: tuple[datetime, datetime] | None = None
    if query._between_dates is not None:
        start_value, end_value = query._between_dates
        start = _parse_datetime_boundary(start_value, is_end=False)
        end = _parse_datetime_boundary(end_value, is_end=True)
        if start > end:
            raise BubError(ErrorKind.INVALID_INPUT, "Start date must be earlier than or equal to end date.")
        date_range = (start, end)

    yielded = 0
    async for record in _window_entries(records, query):
        if query._after_cursor is not None and record.cursor <= query._after_cursor:
            continue
        if date_range is not None and not _record_in_datetime_range(record, *date_range):
            continue
        if query._query and not _record_matches_query(record, query._query):
            continue
        if query._types and record.event.get_type() not in query._types:
            continue
        yield record
        yielded += 1
        if query._limit is not None and yielded >= query._limit:
            return


async def _iter_snapshot(records: list[TapeRecord]) -> AsyncIterator[TapeRecord]:
    for record in records:
        yield record


class InMemoryTapeStore:
    """In-memory implementation of the async tape store contract."""

    def __init__(self) -> None:
        self._tapes: dict[str, list[TapeRecord]] = {}
        self._next_id: dict[str, int] = {}

    async def list_tapes(self) -> list[str]:
        return sorted(self._tapes)

    async def reset(self, tape: str) -> None:
        self._tapes.pop(tape, None)
        self._next_id.pop(tape, None)

    def scan(self, query: TapeQuery) -> AsyncIterator[TapeRecord]:
        snapshot = [record.copy() for record in self._tapes.get(query.tape, ())]
        return _scan_query(_iter_snapshot(snapshot), query)

    async def append(self, tape: str, event: CloudEvent) -> TapeRecord:
        next_id = self._next_id.get(tape, 1)
        self._next_id[tape] = next_id + 1
        stored = TapeRecord(next_id, event)
        self._tapes.setdefault(tape, []).append(stored)
        return stored.copy()


class ForkTapeStore:
    """Write-isolated overlay retained until session contexts own execution scopes."""

    def __init__(self, parent: TapeStore, tape: str, *, sidecars: tuple[str, ...] = ()) -> None:
        self._parent = parent
        self._overlay = InMemoryTapeStore()
        self._tape = tape
        self._sidecars = tuple(dict.fromkeys(sidecars))
        self._managed_tapes = {tape, *self._sidecars}
        self._reset_tapes: set[str] = set()
        self._base_cursors: dict[str, int] = {}
        self._initialize_lock = asyncio.Lock()

    async def _base_cursor(self, tape: str) -> int:
        if tape in self._base_cursors:
            return self._base_cursors[tape]
        async with self._initialize_lock:
            if tape in self._base_cursors:
                return self._base_cursors[tape]
            cursor = 0
            async for record in self._parent.scan(TapeQuery(tape)):
                cursor = record.cursor
            self._base_cursors[tape] = cursor
            return cursor

    async def list_tapes(self) -> list[str]:
        return await self._parent.list_tapes()

    @property
    def reset_tapes(self) -> frozenset[str]:
        return frozenset(self._reset_tapes)

    async def reset(self, tape: str) -> None:
        if tape not in self._managed_tapes:
            await self._parent.reset(tape)
            return
        await self._base_cursor(tape)
        await self._overlay.reset(tape)
        self._reset_tapes.add(tape)

    def scan(self, query: TapeQuery) -> AsyncIterator[TapeRecord]:
        if query.tape not in self._managed_tapes:
            return self._parent.scan(query)

        async def combined() -> AsyncIterator[TapeRecord]:
            raw_query = TapeQuery(query.tape)
            base_cursor = await self._base_cursor(query.tape)
            if query.tape not in self._reset_tapes:
                async for record in self._parent.scan(raw_query):
                    if record.cursor > base_cursor:
                        break
                    yield record
            async for record in self._overlay.scan(raw_query):
                visible_base = 0 if query.tape in self._reset_tapes else base_cursor
                yield record.copy(cursor=visible_base + record.cursor)

        return _scan_query(combined(), query)

    @staticmethod
    def _redacted_data(data: dict) -> dict:
        redacted = copy.deepcopy(data)
        message = redacted.get("message")
        if not isinstance(message, list):
            return redacted
        redacted["message"] = [part for part in message if isinstance(part, dict) and part.get("type") == "text"]
        return redacted

    async def append(self, tape: str, event: CloudEvent) -> TapeRecord:
        redacted = CloudEvent(copy.deepcopy(event.get_attributes()), self._redacted_data(event_data(event)))
        if tape not in self._managed_tapes:
            return await self._parent.append(tape, redacted)
        base_cursor = await self._base_cursor(tape)
        stored = await self._overlay.append(tape, redacted)
        visible_base = 0 if tape in self._reset_tapes else base_cursor
        return stored.copy(cursor=visible_base + stored.cursor)

    async def merge_back(self) -> None:
        total = 0
        for sidecar in self._sidecars:
            try:
                if sidecar in self._reset_tapes:
                    await self._parent.reset(sidecar)
                async for record in self._overlay.scan(TapeQuery(sidecar)):
                    await self._parent.append(sidecar, record.event)
                    total += 1
            except Exception as exc:
                logger.warning('Failed to merge sidecar "{}" into tape "{}": {}', sidecar, self._tape, exc)
                await self._overlay.append(
                    self._tape,
                    bub_event(
                        "sidecar.merge",
                        {"tape": sidecar, "status": "error", "error": str(exc)},
                    ),
                )

        if self._tape in self._reset_tapes:
            await self._parent.reset(self._tape)
        async for record in self._overlay.scan(TapeQuery(self._tape)):
            await self._parent.append(self._tape, record.event)
            total += 1
        if total:
            logger.info('Merged {} entries into tape fork "{}"', total, self._tape)


class FileTapeStore:
    """Line-oriented file persistence with an injected record codec."""

    def __init__(self, directory: Path, *, codec: TapeCodec) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._codec = codec
        self._tape_files: dict[str, TapeFile] = {}

    def _tape_file(self, tape: str) -> TapeFile:
        if tape not in self._tape_files:
            path = self._directory / f"{tape}{self._codec.file_suffix}"
            self._tape_files[tape] = TapeFile(path, self._codec)
        return self._tape_files[tape]

    async def list_tapes(self) -> list[str]:
        suffix = self._codec.file_suffix

        def list_files() -> list[str]:
            return sorted(
                path.name[: -len(suffix)]
                for path in self._directory.glob(f"*{suffix}")
                if path.name[: -len(suffix)].count("__") == 1
            )

        return await asyncio.to_thread(list_files)

    async def reset(self, tape: str) -> None:
        await asyncio.to_thread(self._tape_file(tape).reset)

    def scan(self, query: TapeQuery) -> AsyncIterator[TapeRecord]:
        iterator = self._tape_file(query.tape).iter_entries()
        return _scan_query(iterate_in_thread(iterator), query)

    async def append(self, tape: str, event: CloudEvent) -> TapeRecord:
        return await asyncio.to_thread(self._tape_file(tape).append, event)


class TapeFile:
    """Thread-safe append and snapshot scan operations for one framed tape file."""

    def __init__(self, path: Path, codec: TapeCodec) -> None:
        self.path = path
        self._codec = codec
        self._lock = threading.Lock()
        self._next_id: int | None = None

    def reset(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
            self._next_id = None

    def iter_entries(self) -> Iterator[TapeRecord]:
        with self._lock:
            end_offset = self.path.stat().st_size if self.path.exists() else 0
        return self._iter_entries_until(end_offset)

    def _iter_entries_until(self, end_offset: int) -> Iterator[TapeRecord]:
        if end_offset == 0 or not self.path.exists():
            return
        cursor = 0
        with self.path.open("rb") as handle:
            while handle.tell() < end_offset:
                frame = handle.readline()
                if not frame:
                    return
                if not frame.endswith(b"\n"):
                    return
                payload = frame[:-1]
                if not payload:
                    continue
                cursor += 1
                yield TapeRecord(cursor, self._codec.decode(payload))

    def _repair_tail_locked(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("rb+") as handle:
            handle.seek(-1, 2)
            if handle.read(1) == b"\n":
                return
            position = handle.tell() - 1
            while position >= 0:
                handle.seek(position)
                if handle.read(1) == b"\n":
                    handle.truncate(position + 1)
                    return
                position -= 1
            handle.truncate(0)

    def _load_next_id_locked(self) -> int:
        end_offset = self.path.stat().st_size if self.path.exists() else 0
        next_id = 1
        for record in self._iter_entries_until(end_offset):
            next_id = max(next_id, record.cursor + 1)
        return next_id

    def append(self, event: CloudEvent) -> TapeRecord:
        with self._lock:
            self._repair_tail_locked()
            if self._next_id is None:
                self._next_id = self._load_next_id_locked()
            stored = TapeRecord(self._next_id, event)
            frame = self._codec.encode(stored.event)
            if b"\n" in frame:
                raise TapeCodecError("line tape codecs must encode exactly one newline-free frame")
            with self.path.open("ab") as handle:
                handle.write(frame + b"\n")
            self._next_id += 1
            return stored.copy()
