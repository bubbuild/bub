from __future__ import annotations

import itertools
import json
import re
import threading
from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from bub.tape import (
    AsyncTapeStore,
    InMemoryQueryMixin,
    InMemoryTapeStore,
    TapeEntry,
    TapeQuery,
)
from bub.utils import get_entry_text

WORD_PATTERN = re.compile(r"[a-z0-9_/-]+")
MIN_FUZZY_QUERY_LENGTH = 3
MIN_FUZZY_SCORE = 80
MAX_FUZZY_CANDIDATES = 128


class ForkTapeStore:
    def __init__(self, parent: AsyncTapeStore, tape: str) -> None:
        self._parent = parent
        self._store = InMemoryTapeStore()
        self._tape = tape
        self._tape_was_reset = False

    async def list_tapes(self) -> list[str]:
        return await self._parent.list_tapes()

    async def reset(self, tape: str) -> None:
        if tape != self._tape:
            await self._parent.reset(tape)
            return
        self._store.reset(tape)
        self._tape_was_reset = True

    async def fetch_all(self, query: TapeQuery[AsyncTapeStore]) -> Iterable[TapeEntry]:
        parent_entries: Iterable[TapeEntry] = []
        if not (query.tape == self._tape and self._tape_was_reset):
            try:
                parent_entries = await self._parent.fetch_all(query)
            except Exception:
                parent_entries = []
        this_entries: list[TapeEntry] = []
        for entry in self._store.read(query.tape) or []:
            if query._kinds and entry.kind not in query._kinds:
                continue
            if entry.kind == "anchor":  # noqa: SIM102
                if query._after_last or (query._after_anchor and entry.payload.get("name") == query._after_anchor):
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
        self._store.append(tape, entry)

    async def merge_back(self) -> None:
        if self._tape_was_reset:
            await self._parent.reset(self._tape)
        entries = self._store.read(self._tape)
        if not entries:
            return
        count = len(entries)
        for entry in entries:
            await self._parent.append(self._tape, entry)
        logger.info(f'Merged {count} entries into tape "{self._tape}"')


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
