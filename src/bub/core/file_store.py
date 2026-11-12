"""JSONL log with file-derived byte offsets and instance-local locking.

There are no metadata sidecars, lock files or query indexes. Readers poll for
external writes; only whole lines count. Independent writers must not overlap.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import aclosing, asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from bub.core.tape import TapeEntry
from bub.core.tape_log import TapeOffset, TapeRecord, decode_offset, encode_offset
from bub.core.tape_query import _StreamQuery
from bub.errors import BubError, ErrorKind


class FileTapeStore(_StreamQuery):
    """Async JSONL store. File work is finite and runs outside the event loop."""

    def __init__(self, directory: Path, *, poll_interval: float = 0.05) -> None:
        self._directory = directory.resolve()
        self._poll_interval = poll_interval
        self._files: dict[str, _TapeFile] = {}

    def _file(self, tape: str) -> _TapeFile:
        if not tape or Path(tape).name != tape or tape in {".", ".."}:
            raise BubError(ErrorKind.INVALID_INPUT, "Tape names must be nonempty file names.")
        if tape not in self._files:
            self._files[tape] = _TapeFile(self._directory / f"{tape}.jsonl")
        return self._files[tape]

    async def list_tapes(self) -> list[str]:
        def scan() -> list[str]:
            return sorted(file.stem for file in self._directory.glob("*.jsonl") if file.stem.count("__") == 1)

        return await asyncio.to_thread(scan)

    async def reset(self, tape: str) -> None:
        await asyncio.to_thread(self._file(tape).reset)

    async def append(self, tape: str, entry: TapeEntry) -> TapeRecord:
        return await asyncio.to_thread(self._file(tape).append, entry)

    async def tail(self, tape: str) -> TapeOffset:
        return await asyncio.to_thread(self._file(tape).tail)

    @asynccontextmanager
    async def stream(
        self, tape: str, *, start: TapeOffset | None = None, stop: TapeOffset | None = None
    ) -> AsyncIterator[AsyncIterator[TapeRecord]]:
        file = self._file(tape)
        identity, position, end = await asyncio.to_thread(file.bounds, start, stop)

        async def read() -> AsyncGenerator[TapeRecord, None]:
            cursor = position
            while end is None or cursor < end:
                records, cursor = await asyncio.to_thread(file.scan, identity, cursor, end)
                if records:
                    for record in records:
                        yield record
                elif end is None:
                    await asyncio.sleep(self._poll_interval)
                else:
                    return

        async with aclosing(read()) as records:
            yield records


class _TapeFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            yield

    def _identity(self, handle: BinaryIO) -> str:
        stat = os.fstat(handle.fileno())
        return f"{self.path}:{stat.st_dev}:{stat.st_ino}"

    def tail(self) -> TapeOffset:
        with self._locked(), self.path.open("a+b") as handle:
            return encode_offset(self._identity(handle), _line_boundary(handle, handle.tell()))

    def reset(self) -> None:
        with self._locked():
            # Replace, rather than truncate, so current offsets see a different inode.
            replacement = self.path.with_suffix(".new")
            replacement.write_bytes(b"")
            replacement.replace(self.path)

    def bounds(self, start: TapeOffset | None, stop: TapeOffset | None) -> tuple[str, int, int | None]:
        with self._locked(), self.path.open("a+b") as handle:
            identity = self._identity(handle)
            tail = handle.tell()
            position = decode_offset(start, identity, tail) if start is not None else 0
            end = decode_offset(stop, identity, tail) if stop is not None else None
            if end is not None and position > end:
                raise BubError(ErrorKind.INVALID_INPUT, "Tape start is after stop.")
            return identity, position, end

    def scan(self, identity: str, position: int, end: int | None) -> tuple[list[TapeRecord], int]:
        # One worker opens and seeks a bounded range. Identity/size are checked
        # once here, not per record; handles close before anything is yielded.
        with self._locked(), self.path.open("a+b") as handle:
            size = handle.tell()
            if identity != self._identity(handle) or size < (end if end is not None else position):
                raise BubError(ErrorKind.INVALID_INPUT, "Tape was reset, replaced or truncated while reading.")
            upper = size if end is None else end
            handle.seek(position)
            scan_end = min(upper, position + io.DEFAULT_BUFFER_SIZE)
            records: list[TapeRecord] = []
            while position < upper and (not records or position < scan_end):
                line = handle.readline(upper - position)
                if not line.endswith(b"\n"):
                    break
                position = handle.tell()
                entry = _entry_from_line(line)
                if entry is not None:
                    records.append(TapeRecord(entry, encode_offset(identity, position)))
            return records, position

    def append(self, entry: TapeEntry) -> TapeRecord:
        with self._locked(), self.path.open("a+b") as handle:
            end = handle.tell()
            if _line_boundary(handle, end) != end:
                raise BubError(ErrorKind.INVALID_INPUT, "Cannot append after an incomplete JSONL record.")
            entry_id = _last_entry_id(handle, end) + 1
            stored = TapeEntry(entry_id, entry.kind, dict(entry.payload), dict(entry.meta), entry.date)
            line = (json.dumps(asdict(stored), ensure_ascii=False) + "\n").encode("utf-8")
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            return TapeRecord(stored, encode_offset(self._identity(handle), handle.tell()))


def _entry_from_line(line: bytes) -> TapeEntry | None:
    try:
        payload = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    entry_id, kind, entry_payload = payload.get("id"), payload.get("kind"), payload.get("payload")
    if not isinstance(entry_id, int) or not isinstance(kind, str) or not isinstance(entry_payload, dict):
        return None
    meta = payload.get("meta")
    date = payload.get("date")
    if date is None:
        date = datetime.fromtimestamp(payload.get("timestamp", 0.0), tz=UTC).isoformat()
    return TapeEntry(entry_id, kind, entry_payload, meta if isinstance(meta, dict) else {}, date)


def _line_boundary(handle: BinaryIO, end: int) -> int:
    """Find the last newline at or before end without decoding record history."""
    while end:
        start = max(0, end - 8192)
        handle.seek(start)
        newline = handle.read(end - start).rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0


def _last_entry_id(handle: BinaryIO, end: int) -> int:
    while end:
        start = _line_boundary(handle, end - 1)
        handle.seek(start)
        entry = _entry_from_line(handle.read(end - start))
        if entry is not None:
            return entry.id
        end = start
    return 0
