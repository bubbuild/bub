"""Tape context helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Hashable, Iterable
from typing import Any

from republic import RepublicTapeFormat, TapeContext, TapeEntry

from bub.utils import get_entry_text

_WORD_PATTERN = re.compile(r"[a-z0-9_/-]+")
_MIN_FUZZY_QUERY_LENGTH = 3
_MIN_FUZZY_SCORE = 80
_MAX_FUZZY_CANDIDATES = 128


def default_tape_context() -> TapeContext:
    """Return the default context selection for Bub."""

    return TapeContext()


def default_tape_format() -> BubTapeFormat:
    """Return Bub's default tape format."""

    return BubTapeFormat()


class BubTapeFormat(RepublicTapeFormat):
    """Bub's default tape format and injection rules."""

    name = "bub"
    version = "1"

    def select_messages(self, entries: Iterable[TapeEntry], context: object) -> list[dict[str, Any]]:
        del context
        messages: list[dict[str, Any]] = []
        pending_calls: list[dict[str, Any]] = []

        for entry in entries:
            match entry.kind:
                case "anchor":
                    _append_anchor_entry(messages, entry)
                case "message":
                    _append_message_entry(messages, entry)
                case "tool_call":
                    pending_calls = _append_tool_call_entry(messages, entry)
                case "tool_result":
                    _append_tool_result_entry(messages, pending_calls, entry)
                    pending_calls = []
        return messages

    def matches(self, entry: TapeEntry, query: str) -> bool:
        needle = query.strip().casefold()
        if not needle:
            return False
        haystack = get_entry_text(entry).casefold()
        return needle in haystack or _is_fuzzy_match(needle, haystack)

    def dedup_key(self, entry: TapeEntry) -> Hashable:
        return get_entry_text(entry).casefold()


def _append_anchor_entry(messages: list[dict[str, Any]], entry: TapeEntry) -> None:
    payload = entry.payload
    content = f"[Anchor created: {payload.get('name')}]: {json.dumps(payload.get('state'), ensure_ascii=False)}"
    messages.append({"role": "assistant", "content": content})


def _append_message_entry(messages: list[dict[str, Any]], entry: TapeEntry) -> None:
    payload = entry.payload
    if isinstance(payload, dict):
        messages.append(dict(payload))


def _append_tool_call_entry(messages: list[dict[str, Any]], entry: TapeEntry) -> list[dict[str, Any]]:
    calls = _normalize_tool_calls(entry.payload.get("calls"))
    if calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
    return calls


def _append_tool_result_entry(
    messages: list[dict[str, Any]],
    pending_calls: list[dict[str, Any]],
    entry: TapeEntry,
) -> None:
    results = entry.payload.get("results")
    if not isinstance(results, list):
        return
    for index, result in enumerate(results):
        messages.append(_build_tool_result_message(result, pending_calls, index))


def _build_tool_result_message(
    result: object,
    pending_calls: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "tool", "content": _render_tool_result(result)}
    if index >= len(pending_calls):
        return message

    call = pending_calls[index]
    call_id = call.get("id")
    if isinstance(call_id, str) and call_id:
        message["tool_call_id"] = call_id

    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name:
            message["name"] = name
    return message


def _normalize_tool_calls(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            calls.append(dict(item))
    return calls


def _render_tool_result(result: object) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except TypeError:
        return str(result)


def _is_fuzzy_match(needle: str, haystack: str) -> bool:
    if len(needle) < _MIN_FUZZY_QUERY_LENGTH:
        return False

    from rapidfuzz import fuzz, process

    query_tokens = _WORD_PATTERN.findall(needle)
    if not query_tokens:
        return False
    source_tokens = _WORD_PATTERN.findall(haystack)
    if not source_tokens:
        return False

    candidates = source_tokens[:_MAX_FUZZY_CANDIDATES]
    window_size = len(query_tokens)
    if window_size > 1:
        for idx in range(max(0, len(source_tokens) - window_size + 1)):
            candidates.append(" ".join(source_tokens[idx : idx + window_size]))
            if len(candidates) >= _MAX_FUZZY_CANDIDATES:
                break

    return (
        process.extractOne(
            " ".join(query_tokens),
            candidates,
            scorer=fuzz.WRatio,
            score_cutoff=_MIN_FUZZY_SCORE,
        )
        is not None
    )
