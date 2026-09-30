"""Per-session settings persisted as switch events on the session tape."""

from __future__ import annotations

from typing import Any

from bub.tape import Tape
from bub.tools import ToolContext

SESSION_SETTINGS = ("model", "reasoning_effort", "code_mode")


def _switch_event(key: str) -> str:
    return f"{key}_switch"


async def set_session_setting(context: ToolContext, key: str, value: Any) -> None:
    """Apply a session setting to the current state and record it on the session tape.

    The event is merged back with the turn, so ``load_session_settings`` restores the
    value on the next turn and after restarts.
    """
    if key not in SESSION_SETTINGS:
        raise ValueError(f"unknown session setting: {key!r}")
    context.state[key] = value
    await context.tape.append_event(_switch_event(key), {key: value})


async def load_session_settings(tape: Tape) -> dict[str, Any]:
    """Return the latest recorded value of each session setting; empty values clear it."""
    keys = {_switch_event(key): key for key in SESSION_SETTINGS}
    settings: dict[str, Any] = {}
    for entry in await tape.store.fetch_all(tape.query().kinds("event")):
        key = keys.get(entry.payload.get("name", ""))
        data = entry.payload.get("data")
        if key is None or not isinstance(data, dict) or key not in data:
            continue
        if data[key] is None or data[key] == "":
            settings.pop(key, None)
        else:
            settings[key] = data[key]
    return settings
