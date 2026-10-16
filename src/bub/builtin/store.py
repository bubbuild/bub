"""File tape store with the newest-first, deduplicated and fuzzy text search used by the builtin tools."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace

from bub.core.store import FileTapeStore, TapeQuery
from bub.core.tape import TapeEntry
from bub.utils import get_entry_text

WORD_PATTERN = re.compile(r"[a-z0-9_/-]+")
MIN_FUZZY_QUERY_LENGTH = 3
MIN_FUZZY_SCORE = 80
MAX_FUZZY_CANDIDATES = 128


class FuzzyFileTapeStore(FileTapeStore):
    """:class:`~bub.core.store.FileTapeStore` whose text queries also match approximately."""

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        if not query._query:
            result: Iterable[TapeEntry] = super().fetch_all(query)
            return result
        unlimited_query = replace(query, _query=None, _limit=None)
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
