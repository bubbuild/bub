from __future__ import annotations

from pathlib import Path

import pytest

from bub.store import FileTapeStore, TapeQuery
from bub.tape import TapeEntry


@pytest.mark.parametrize(
    ("content", "query", "matches"),
    [
        ("deployment configuration", "deployment", True),
        ("Deployment configuration", "DEPLOYMENT", True),
        ("deployment configuration", "deploymnt", True),
        ("项目配置和部署说明", "项目配置", True),
        ("café configuration", "CAFÉ", True),
        ("deployment 🚀 ready", "🚀", True),
        ("deployment configuration", "zqxjkv", False),
        ("deployment configuration", "xy", False),
        ("deployment configuration", "   ", False),
    ],
)
def test_file_search_matches_persisted_payload(tmp_path: Path, content: str, query: str, matches: bool) -> None:
    store = FileTapeStore(tmp_path)
    store.append("session", TapeEntry.message({"role": "user", "content": content}))

    # Reopen the store so the query exercises persisted data rather than its write cache.
    reopened = FileTapeStore(tmp_path)
    search = TapeQuery("session", reopened).query(query)
    entries = list(search.all())

    assert [entry.payload["content"] for entry in entries] == ([content] if matches else [])
    assert search._query == query


@pytest.mark.parametrize("limit", [1, 2, 10])
def test_fuzzy_search_preserves_filters_order_deduplication_and_limit(tmp_path: Path, limit: int) -> None:
    store = FileTapeStore(tmp_path)
    entries = [
        TapeEntry.message({"content": "deployment before anchor"}),
        TapeEntry.anchor("start"),
        TapeEntry(0, "message", {"content": "deployment alpha"}, date="2026-01-02T00:00:00Z"),
        TapeEntry(0, "message", {"content": "deployment alpha"}, date="2026-01-02T12:00:00Z"),
        TapeEntry(0, "message", {"content": "deployment beta"}, date="2026-01-03T23:59:59Z"),
        TapeEntry(0, "system", {"content": "deployment system"}, date="2026-01-03T00:00:00Z"),
        TapeEntry(0, "message", {"content": "deployment outside dates"}, date="2026-01-01T00:00:00Z"),
        TapeEntry.anchor("end"),
        TapeEntry.message({"content": "deployment after anchor"}),
    ]
    for entry in entries:
        store.append("session", entry)
    search = (
        TapeQuery("session", store)
        .between_anchors("start", "end")
        .between_dates("2026-01-02", "2026-01-03")
        .kinds("message")
        .query("deploymnt")
        .limit(limit)
    )

    assert [entry.id for entry in search.all()] == [5, 4][:limit]
    assert search._query == "deploymnt"
    assert search._limit == limit


@pytest.mark.parametrize("last", [False, True])
def test_fuzzy_search_preserves_after_anchor_selection(tmp_path: Path, last: bool) -> None:
    store = FileTapeStore(tmp_path)
    store.append("session", TapeEntry.message({"content": "deployment before"}))
    store.append("session", TapeEntry.anchor("checkpoint"))
    store.append("session", TapeEntry.message({"content": "deployment after"}))
    search = TapeQuery("session", store).query("deploymnt")
    search = search.last_anchor() if last else search.after_anchor("checkpoint")

    assert [entry.id for entry in search.all()] == [3]


def test_unqueried_file_search_retains_chronological_order(tmp_path: Path) -> None:
    store = FileTapeStore(tmp_path)
    for content in ("first", "second", "third"):
        store.append("session", TapeEntry.message({"content": content}))

    assert [entry.id for entry in TapeQuery("session", store).limit(2).all()] == [1, 2]
