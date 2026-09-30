from __future__ import annotations

from pathlib import Path

import pytest

from bub.builtin.session_settings import load_session_settings, set_session_setting
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.tape import Tape, TapeContext
from bub.tools import ToolContext


def _context(tmp_path: Path) -> ToolContext:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("session")
    return ToolContext(tape=tape, run_id="run-1", state={})


@pytest.mark.asyncio
async def test_session_settings_round_trip_through_switch_events(tmp_path: Path) -> None:
    context = _context(tmp_path)

    await set_session_setting(context, "model", "openai:gpt-4o")
    await set_session_setting(context, "reasoning_effort", "high")
    await set_session_setting(context, "code_mode", True)
    await set_session_setting(context, "code_mode", False)

    assert context.state == {"model": "openai:gpt-4o", "reasoning_effort": "high", "code_mode": False}
    assert await load_session_settings(context.tape) == context.state
    events = list(await context.tape.store.fetch_all(context.tape.query().kinds("event")))
    assert events[0].payload == {"name": "model_switch", "data": {"model": "openai:gpt-4o"}}


@pytest.mark.asyncio
async def test_load_session_settings_ignores_unrelated_events_and_clears_empty_values(tmp_path: Path) -> None:
    tape = _context(tmp_path).tape
    await tape.append_event("model_switch", {"model": "openai:gpt-4o"})
    await tape.append_event("model_switch", {"model": ""})
    await tape.append_event("reasoning_effort_switch", {"unexpected": "high"})
    await tape.append_event("other_switch", {"other": 1})

    assert await load_session_settings(tape) == {}


@pytest.mark.asyncio
async def test_set_session_setting_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown session setting"):
        await set_session_setting(_context(tmp_path), "temperature", 0.5)
