from __future__ import annotations

import contextlib
import contextvars
import itertools
import json
import threading
from collections.abc import AsyncGenerator, Hashable, Iterable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger
from republic import AsyncTapeStore, TapeEntry, TapeFormat, TapeQuery
from republic.tape import AsyncTapeStoreAdapter, InMemoryQueryMixin, InMemoryTapeStore, TapeStore
from republic.tape.store import is_async_tape_store

current_store: contextvars.ContextVar[TapeStore] = contextvars.ContextVar("current_store")
current_fork_tape: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_fork_tape", default=None)
current_tape_was_reset: contextvars.ContextVar[bool] = contextvars.ContextVar("current_tape_was_reset", default=False)


class ForkTapeStore:
    def __init__(self, parent: AsyncTapeStore | TapeStore) -> None:
        if is_async_tape_store(parent):
            self._parent = parent
        else:
            self._parent = AsyncTapeStoreAdapter(parent)

    @property
    def _current(self) -> TapeStore:
        return current_store.get(_empty_store)

    @property
    def _fork_tape(self) -> str | None:
        return current_fork_tape.get()

    @property
    def _current_was_reset(self) -> bool:
        return current_tape_was_reset.get()

    async def list_tapes(self) -> list[str]:
        return cast(list[str], await self._parent.list_tapes())

    async def reset(self, tape: str) -> None:
        self._current.reset(tape)
        if self._current is _empty_store or self._fork_tape != tape:
            await self._parent.reset(tape)
            return
        current_tape_was_reset.set(True)

    async def fetch_all(self, query: TapeQuery[AsyncTapeStore]) -> Iterable[TapeEntry]:
        parent_entries: Iterable[TapeEntry] = []
        if not (query.tape == self._fork_tape and self._current_was_reset):
            try:
                parent_entries = await self._parent.fetch_all(query)
            except Exception:
                parent_entries = []
        this_entries: list[TapeEntry] = []
        if hasattr(self._current, "read"):
            for entry in cast(list[TapeEntry], self._current.read(query.tape) or []):
                if query._kinds and query.tape_format.entry_kind(entry) not in query._kinds:
                    continue
                anchor_name = query.tape_format.anchor_name(entry)
                if anchor_name is not None:  # noqa: SIM102
                    if query._after_last or (query._after_anchor and anchor_name == query._after_anchor):
                        this_entries.clear()
                        parent_entries = []
                        continue
                this_entries.append(entry)
        return itertools.chain(parent_entries, this_entries)

    @staticmethod
    def _redact_prompt(prompt: list[dict]) -> Any:
        if not isinstance(prompt, list):
            return prompt
        new_prompt = []
        for part in prompt:
            if part.get("type") == "text":
                new_prompt.append(part)
        return new_prompt

    @staticmethod
    def _redact_payload(payload: dict) -> None:
        if "content" in payload:
            payload["content"] = ForkTapeStore._redact_prompt(payload["content"])
        elif "prompt" in payload:
            payload["prompt"] = ForkTapeStore._redact_prompt(payload["prompt"])

    async def append(self, tape: str, entry: TapeEntry) -> None:
        self._redact_payload(entry.payload)
        self._current.append(tape, entry)

    @contextlib.asynccontextmanager
    async def fork(self, tape: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        store = InMemoryTapeStore()
        token = current_store.set(store)
        tape_token = current_fork_tape.set(tape)
        reset_token = current_tape_was_reset.set(False)
        try:
            yield
        finally:
            was_reset = current_tape_was_reset.get()
            current_store.reset(token)
            current_fork_tape.reset(tape_token)
            current_tape_was_reset.reset(reset_token)
            if merge_back:
                if was_reset:
                    await self._parent.reset(tape)
                entries = store.read(tape)
                if entries:
                    count = len(entries)
                    for entry in entries:
                        await self._parent.append(tape, entry)
                    logger.info(f'Merged {count} entries into tape "{tape}"')


class EmptyTapeStore:
    """Sync TapeStore sentinel that always returns empty results."""

    def list_tapes(self) -> list[str]:
        return []

    def reset(self, tape: str) -> None:
        pass

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        return []

    def append(self, tape: str, entry: TapeEntry) -> None:
        pass


_empty_store = EmptyTapeStore()


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
        entries = list(cast(Iterable[TapeEntry], super().fetch_all(unlimited_query)))
        return self._dedup_recent(entries, query.tape_format, query._limit or 20)

    @staticmethod
    def _dedup_recent(entries: list[TapeEntry], tape_format: TapeFormat, limit: int) -> list[TapeEntry]:
        results: list[TapeEntry] = []
        seen: set[Hashable] = set()
        for entry in reversed(entries):
            key = tape_format.dedup_key(entry)
            if key in seen:
                continue
            seen.add(key)
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    def _tape_file(self, tape: str) -> TapeFile:
        if tape not in self._tape_files:
            self._tape_files[tape] = TapeFile(self._directory / f"{tape}.jsonl")
        return self._tape_files[tape]

    def list_tapes(self) -> list[str]:
        result: list[str] = []
        for file in self._directory.glob("*.jsonl"):
            filename = file.stem
            if filename.count("__") != 1:
                continue
            result.append(filename)
        return result

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
            return cast(int, self._read_entries[-1].id + 1)
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
