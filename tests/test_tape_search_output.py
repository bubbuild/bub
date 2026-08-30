from __future__ import annotations

from datetime import UTC, datetime

import pytest

import bub.builtin.tools as builtin_tools
from bub.builtin.tools import tape_search
from bub.tape import TapeRecord, bub_event
from bub.tools import ToolContext


class _FakeTapes:
    def __init__(self, entries: list[TapeRecord]) -> None:
        self._entries = entries

    def scoped(self, _tape: str) -> _FakeTapes:
        return self

    def query(self) -> _FakeQuery:
        return _FakeQuery()

    async def stream(self, _query: object):
        for entry in self._entries:
            yield entry


class _FakeQuery:
    def query(self, _value: str) -> _FakeQuery:
        return self

    def types(self, *_types: str) -> _FakeQuery:
        return self

    def limit(self, _value: int) -> _FakeQuery:
        return self

    def between_dates(self, _start: str, _end: str) -> _FakeQuery:
        return self


class _FakeAgent:
    def __init__(self, entries: list[TapeRecord]) -> None:
        self.tapes = _FakeTapes(entries)


@pytest.mark.asyncio
async def test_tape_search_reports_shown_matches_and_filtered_count(monkeypatch) -> None:
    entries = [
        TapeRecord(1, bub_event("message", {"content": "ok"}, time=datetime(2026, 1, 1, tzinfo=UTC))),
        TapeRecord(
            2,
            bub_event(
                "message",
                {"content": "[tape.search]: 1 matches"},
                time=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            ),
        ),
    ]
    monkeypatch.setattr(builtin_tools, "_get_agent", lambda _context: _FakeAgent(entries))

    output = await tape_search.run(
        query="x", context=ToolContext(tape=_FakeTapes(entries), model_call_id="model", state={})
    )

    assert output.splitlines()[0] == "[tape.search]: 1 matches (1 filtered)"


@pytest.mark.asyncio
async def test_tape_search_reports_zero_filtered_explicitly(monkeypatch) -> None:
    entries = [
        TapeRecord(1, bub_event("message", {"content": "a"}, time=datetime(2026, 1, 1, tzinfo=UTC))),
        TapeRecord(
            2,
            bub_event("message", {"content": "b"}, time=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)),
        ),
    ]
    monkeypatch.setattr(builtin_tools, "_get_agent", lambda _context: _FakeAgent(entries))

    output = await tape_search.run(
        query="x", context=ToolContext(tape=_FakeTapes(entries), model_call_id="model", state={})
    )

    assert output.splitlines()[0] == "[tape.search]: 2 matches (0 filtered)"
