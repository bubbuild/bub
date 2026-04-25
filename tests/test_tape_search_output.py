from __future__ import annotations

from dataclasses import dataclass

import pytest
from republic import ToolContext

import bub.builtin.tools as builtin_tools
from bub.builtin.tools import tape_search


@dataclass(frozen=True)
class _FakeEntry:
    date: str
    payload: object


class _FakeTapes:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self._entries = entries
        self._store = object()

    async def search(self, _query: object) -> list[_FakeEntry]:
        return list(self._entries)

    def query(self, _tape_name: str) -> _FakeQuery:
        return _FakeQuery()


class _FakeQuery:
    def query(self, _value: str) -> _FakeQuery:
        return self

    def kinds(self, *_kinds: str) -> _FakeQuery:
        return self

    def limit(self, _value: int) -> _FakeQuery:
        return self

    def between_dates(self, _start: str, _end: str) -> _FakeQuery:
        return self


class _FakeAgent:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self.tapes = _FakeTapes(entries)


@pytest.mark.asyncio
async def test_tape_search_reports_shown_matches_and_filtered_count(monkeypatch) -> None:
    entries = [
        _FakeEntry(date="2026-01-01T00:00:00Z", payload={"content": "ok"}),
        _FakeEntry(date="2026-01-01T00:00:01Z", payload={"content": "[tape.search]: 1 matches"}),
    ]
    monkeypatch.setattr(builtin_tools, "_get_agent", lambda _context: _FakeAgent(entries))

    output = await tape_search.run(query="x", context=ToolContext(tape="tape", run_id="run", state={}))

    assert output.splitlines()[0] == "[tape.search]: 1 matches (1 filtered)"


@pytest.mark.asyncio
async def test_tape_search_reports_zero_filtered_explicitly(monkeypatch) -> None:
    entries = [
        _FakeEntry(date="2026-01-01T00:00:00Z", payload={"content": "a"}),
        _FakeEntry(date="2026-01-01T00:00:01Z", payload={"content": "b"}),
    ]
    monkeypatch.setattr(builtin_tools, "_get_agent", lambda _context: _FakeAgent(entries))

    output = await tape_search.run(query="x", context=ToolContext(tape="tape", run_id="run", state={}))

    assert output.splitlines()[0] == "[tape.search]: 2 matches (0 filtered)"
