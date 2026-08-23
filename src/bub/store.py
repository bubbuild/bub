from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from collections.abc import Coroutine, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, time
from datetime import date as date_type
from pathlib import Path
from typing import NoReturn, Protocol, Self, overload

from typing_extensions import TypeIs

from bub.errors import BubError, ErrorKind
from bub.tape import TapeEntry
from bub.utils import get_entry_text

WORD_PATTERN = re.compile(r"[a-z0-9_/-]+")
MIN_FUZZY_QUERY_LENGTH = 3
MIN_FUZZY_SCORE = 80
MAX_FUZZY_CANDIDATES = 128


class TapeStore(Protocol):
    """Append-only tape storage interface."""

    def list_tapes(self) -> list[str]: ...

    def reset(self, tape: str) -> None: ...

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]: ...

    def append(self, tape: str, entry: TapeEntry) -> None: ...


class AsyncTapeStore(Protocol):
    """Async append-only tape storage interface."""

    async def list_tapes(self) -> list[str]: ...

    async def reset(self, tape: str) -> None: ...

    async def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]: ...

    async def append(self, tape: str, entry: TapeEntry) -> None: ...


def is_async_tape_store(store: TapeStore | AsyncTapeStore) -> TypeIs[AsyncTapeStore]:
    return hasattr(store, "append") and inspect.iscoroutinefunction(store.append)


@dataclass(frozen=True)
class TapeQuery[T: TapeStore | AsyncTapeStore]:
    tape: str
    store: T
    _query: str | None = None
    _after_anchor: str | None = None
    _after_last: bool = False
    _between_anchors: tuple[str, str] | None = None
    _between_dates: tuple[str, str] | None = None
    _kinds: tuple[str, ...] = field(default_factory=tuple)
    _limit: int | None = None

    def query(self, value: str) -> Self:
        return replace(self, _query=value)

    def after_anchor(self, name: str) -> Self:
        if not name:
            return replace(self, _after_anchor=None, _after_last=False)
        return replace(self, _after_anchor=name, _after_last=False)

    def last_anchor(self) -> Self:
        return replace(self, _after_anchor=None, _after_last=True)

    def between_anchors(self, start: str, end: str) -> Self:
        return replace(self, _between_anchors=(start, end))

    def between_dates(self, start: str | date_type, end: str | date_type) -> Self:
        start_value = start.isoformat() if isinstance(start, date_type) else start
        end_value = end.isoformat() if isinstance(end, date_type) else end
        return replace(self, _between_dates=(start_value, end_value))

    def kinds(self, *kinds: str) -> Self:
        return replace(self, _kinds=kinds)

    def limit(self, value: int) -> Self:
        return replace(self, _limit=value)

    @overload
    def all(self: TapeQuery[TapeStore]) -> Iterable[TapeEntry]: ...

    @overload
    async def all(self: TapeQuery[AsyncTapeStore]) -> Iterable[TapeEntry]: ...

    def all(self) -> Iterable[TapeEntry] | Coroutine[None, None, Iterable[TapeEntry]]:
        return self.store.fetch_all(self)


def _anchor_index(
    entries: Sequence[TapeEntry],
    name: str | None,
    *,
    default: int,
    forward: bool,
    start: int = 0,
) -> int:
    rng = range(start, len(entries)) if forward else range(len(entries) - 1, start - 1, -1)
    for idx in rng:
        entry = entries[idx]
        if entry.kind != "anchor":
            continue
        if name is not None and entry.payload.get("name") != name:
            continue
        return idx
    return default


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


class InMemoryQueryMixin:
    """Mixin to implement in-memory query support for simple stores."""

    def read(self, tape: str) -> list[TapeEntry] | None:
        raise NotImplementedError("InMemoryQueryMixin requires a read() method to be implemented.")

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:  # noqa: C901
        entries = self.read(query.tape) or []
        start_index = 0
        end_index: int | None = None

        if query._between_anchors is not None:
            start_name, end_name = query._between_anchors
            start_idx = _anchor_index(entries, start_name, default=-1, forward=False)
            if start_idx < 0:
                raise BubError(ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found.")
            end_idx = _anchor_index(entries, end_name, default=-1, forward=True, start=start_idx + 1)
            if end_idx < 0:
                raise BubError(ErrorKind.NOT_FOUND, f"Anchor '{end_name}' was not found.")
            start_index = min(start_idx + 1, len(entries))
            end_index = min(max(start_index, end_idx), len(entries))
        elif query._after_last:
            anchor_index = _anchor_index(entries, None, default=-1, forward=False)
            if anchor_index < 0:
                raise BubError(ErrorKind.NOT_FOUND, "No anchors found in tape.")
            start_index = min(anchor_index + 1, len(entries))
        elif query._after_anchor is not None:
            anchor_index = _anchor_index(entries, query._after_anchor, default=-1, forward=False)
            if anchor_index < 0:
                raise BubError(ErrorKind.NOT_FOUND, f"Anchor '{query._after_anchor}' was not found.")
            start_index = min(anchor_index + 1, len(entries))

        sliced = entries[start_index:end_index]
        if query._between_dates is not None:
            start_date, end_date = query._between_dates
            start_dt = _parse_datetime_boundary(start_date, is_end=False)
            end_dt = _parse_datetime_boundary(end_date, is_end=True)
            if start_dt > end_dt:
                raise BubError(ErrorKind.INVALID_INPUT, "Start date must be earlier than or equal to end date.")
            sliced = [entry for entry in sliced if _entry_in_datetime_range(entry, start_dt, end_dt)]
        if query._query:
            sliced = [entry for entry in sliced if _entry_matches_query(entry, query._query)]
        if query._kinds:
            sliced = [entry for entry in sliced if entry.kind in query._kinds]
        if query._limit is not None:
            sliced = sliced[: query._limit]
        return sliced


class InMemoryTapeStore(InMemoryQueryMixin):
    """In-memory tape storage."""

    def __init__(self) -> None:
        self._tapes: dict[str, list[TapeEntry]] = {}
        self._next_id: dict[str, int] = {}

    def list_tapes(self) -> list[str]:
        return sorted(self._tapes.keys())

    def reset(self, tape: str) -> None:
        self._tapes.pop(tape, None)
        self._next_id.pop(tape, None)

    def read(self, tape: str) -> list[TapeEntry] | None:
        entries = self._tapes.get(tape)
        if entries is None:
            return None
        return [entry.copy() for entry in entries]

    def append(self, tape: str, entry: TapeEntry) -> None:
        next_id = self._next_id.get(tape, 1)
        self._next_id[tape] = next_id + 1
        stored = TapeEntry(next_id, entry.kind, dict(entry.payload), dict(entry.meta), entry.date)
        self._tapes.setdefault(tape, []).append(stored)


class AsyncTapeStoreAdapter:
    """Adapt a sync TapeStore to AsyncTapeStore."""

    def __init__(self, store: TapeStore) -> None:
        self._store = store

    async def list_tapes(self) -> list[str]:
        return await asyncio.to_thread(self._store.list_tapes)

    async def reset(self, tape: str) -> None:
        await asyncio.to_thread(self._store.reset, tape)

    async def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        return await asyncio.to_thread(self._store.fetch_all, query)

    async def append(self, tape: str, entry: TapeEntry) -> None:
        await asyncio.to_thread(self._store.append, tape, entry)


class UnavailableTapeStore:
    """Sync TapeStore sentinel that always fails with a clear message."""

    def __init__(self, message: str) -> None:
        self._message = message

    def _raise(self) -> NoReturn:
        raise BubError(ErrorKind.INVALID_INPUT, self._message)

    def list_tapes(self) -> list[str]:
        self._raise()

    def reset(self, tape: str) -> None:
        self._raise()

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        self._raise()

    def append(self, tape: str, entry: TapeEntry) -> None:
        self._raise()


class FileTapeStore(InMemoryQueryMixin):
    """TapeStore implementation that persists tapes as JSONL files under a directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._tape_files: dict[str, TapeFile] = {}

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        if not query._query:
            result: Iterable[TapeEntry] = super().fetch_all(query)
            return result
        unlimited_query = replace(query, _limit=None)
        entries: Iterable[TapeEntry] = super().fetch_all(unlimited_query)
        return self._filter_entries(list(entries), query._query, query._limit or 20)

    def _filter_entries(self, entries: list[TapeEntry], query: str, limit: int) -> list[TapeEntry]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []
        results: list[TapeEntry] = []
        seen: set[str] = set()

        count = 0
        for entry in reversed(entries):
            payload_text = get_entry_text(entry).lower()
            if payload_text in seen:
                continue
            seen.add(payload_text)

            if normalized_query in payload_text or self._is_fuzzy_match(normalized_query, payload_text):
                results.append(entry)
                count += 1
                if count >= limit:
                    break
        return results

    @staticmethod
    def _is_fuzzy_match(normalized_query: str, payload_text: str) -> bool:
        from rapidfuzz import fuzz, process

        if len(normalized_query) < MIN_FUZZY_QUERY_LENGTH:
            return False

        query_tokens = WORD_PATTERN.findall(normalized_query)
        if not query_tokens:
            return False
        query_phrase = " ".join(query_tokens)
        window_size = len(query_tokens)

        source_tokens = WORD_PATTERN.findall(payload_text)
        if not source_tokens:
            return False

        candidates: list[str] = []
        for token in source_tokens:
            candidates.append(token)
            if len(candidates) >= MAX_FUZZY_CANDIDATES:
                break

        if window_size > 1:
            max_window_start = len(source_tokens) - window_size + 1
            for idx in range(max(0, max_window_start)):
                candidates.append(" ".join(source_tokens[idx : idx + window_size]))
                if len(candidates) >= MAX_FUZZY_CANDIDATES:
                    break

        best_match = process.extractOne(
            query_phrase,
            candidates,
            scorer=fuzz.WRatio,
            score_cutoff=MIN_FUZZY_SCORE,
        )
        return best_match is not None

    def _tape_file(self, tape: str) -> TapeFile:
        if tape not in self._tape_files:
            self._tape_files[tape] = TapeFile(self._directory / f"{tape}.jsonl")
        return self._tape_files[tape]

    def list_tapes(self) -> list[str]:
        return sorted(file.stem for file in self._directory.glob("*.jsonl") if file.stem.count("__") == 1)

    def reset(self, tape: str) -> None:
        self._tape_file(tape).reset()

    def append(self, tape: str, entry: TapeEntry) -> None:
        self._tape_file(tape).append(entry)

    def read(self, tape: str) -> list[TapeEntry] | None:
        return self._tape_file(tape).read()


class TapeFile:
    """Helper for one tape file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._read_entries: list[TapeEntry] = []
        self._read_offset = 0

    def _next_id(self) -> int:
        if self._read_entries:
            return self._read_entries[-1].id + 1
        return 1

    def _reset(self) -> None:
        self._read_entries = []
        self._read_offset = 0

    def reset(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()
            self._reset()

    def read(self) -> list[TapeEntry]:
        with self._lock:
            return self._read_locked()

    def _read_locked(self) -> list[TapeEntry]:
        if not self.path.exists():
            self._reset()
            return []

        file_size = self.path.stat().st_size
        if file_size < self._read_offset:
            # The file was truncated or replaced, so cached entries are stale.
            self._reset()

        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self._read_offset)
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = self.entry_from_payload(payload)
                if entry is not None:
                    self._read_entries.append(entry)
            self._read_offset = handle.tell()

        return list(self._read_entries)

    @staticmethod
    def entry_from_payload(payload: object) -> TapeEntry | None:
        if not isinstance(payload, dict):
            return None
        entry_id = payload.get("id")
        kind = payload.get("kind")
        entry_payload = payload.get("payload")
        meta = payload.get("meta")
        if not isinstance(entry_id, int):
            return None
        if not isinstance(kind, str):
            return None
        if not isinstance(entry_payload, dict):
            return None
        if not isinstance(meta, dict):
            meta = {}
        if "date" in payload:
            date = payload["date"]
        else:
            date = datetime.fromtimestamp(payload.get("timestamp", 0.0), tz=UTC).isoformat()
        return TapeEntry(entry_id, kind, dict(entry_payload), dict(meta), date)

    def append(self, entry: TapeEntry) -> None:
        with self._lock:
            # Keep cache and offset in sync before allocating new IDs.
            self._read_locked()
            with self.path.open("a", encoding="utf-8") as handle:
                next_id = self._next_id()
                stored = TapeEntry(next_id, entry.kind, dict(entry.payload), dict(entry.meta), entry.date)
                handle.write(json.dumps(asdict(stored), ensure_ascii=False) + "\n")
                self._read_entries.append(stored)
                self._read_offset = handle.tell()
