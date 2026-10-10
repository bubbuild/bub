"""Tape context helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import republic

from bub.tape import TapeContext, TapeEntry, to_message
from bub.tools import render_result


def default_tape_context() -> TapeContext:
    """Return the default context selection for Bub."""

    return TapeContext(select=_select_messages)


def _select_messages(entries: Iterable[TapeEntry], _context: TapeContext) -> list[republic.Message]:
    messages: list[republic.Message] = []
    calls: dict[str, republic.ToolCall] = {}
    pending_calls: list[republic.ToolCall] = []

    for entry in entries:
        match entry.kind:
            case "anchor":
                messages.append(_anchor_message(entry))
            case "message":
                if isinstance(entry.payload, dict):
                    messages.append(to_message(entry.payload, calls))
            case "tool_call":
                pending_calls = _append_tool_call_entry(messages, calls, entry)
            case "tool_result":
                _append_tool_result_entry(messages, pending_calls, entry)
                pending_calls = []
    return messages


def _anchor_message(entry: TapeEntry) -> republic.Message:
    payload = entry.payload
    content = f"[Anchor created: {payload.get('name')}]: {json.dumps(payload.get('state'), ensure_ascii=False)}"
    return republic.assistant(content)


def _append_tool_call_entry(
    messages: list[republic.Message], calls: dict[str, republic.ToolCall], entry: TapeEntry
) -> list[republic.ToolCall]:
    raw_calls = entry.payload.get("calls")
    if not isinstance(raw_calls, list) or not (tool_calls := [item for item in raw_calls if isinstance(item, dict)]):
        return []
    fields = {key: entry.payload[key] for key in ("reasoning", "provider_data") if key in entry.payload}
    message = to_message(
        {"role": "assistant", "content": entry.payload.get("content") or "", "tool_calls": tool_calls, **fields},
        calls,
    )
    messages.append(message)
    return list(message.tool_calls)


def _append_tool_result_entry(
    messages: list[republic.Message], pending_calls: list[republic.ToolCall], entry: TapeEntry
) -> None:
    results: Any = entry.payload.get("results")
    if not isinstance(results, list):
        return
    # Results without a recorded call cannot be expressed as tool messages.
    for call, result in zip(pending_calls, results, strict=False):
        messages.append(republic.tool(call, render_result(result)))
