"""Tape context helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from bub.tape import TapeContext, TapeRecord, bub_event_type, event_data, event_payload


def select_messages(records: Iterable[TapeRecord], _context: TapeContext) -> list[dict[str, Any]]:
    """Map ATIF-native tape records to the model's message vocabulary."""

    messages: list[dict[str, Any]] = []
    anchor_type = bub_event_type("context.anchor")
    message_type = bub_event_type("message")

    for record in records:
        event_type = record.event.get_type()
        if event_type == anchor_type:
            _append_anchor_record(messages, record)
        elif event_type == message_type:
            _append_message_record(messages, record)
    return messages


def _append_anchor_record(messages: list[dict[str, Any]], record: TapeRecord) -> None:
    data = event_payload(record.event)
    content = f"[Anchor created: {data.get('name')}]: {json.dumps(data.get('state'), ensure_ascii=False)}"
    messages.append({"role": "assistant", "content": content})


def _append_message_record(messages: list[dict[str, Any]], record: TapeRecord) -> None:
    data = event_data(record.event)
    source = data.get("source")
    role = "assistant" if source == "agent" else "system" if source == "system" else "user"
    message: dict[str, Any] = {"role": role, "content": _provider_content(data.get("message", ""))}
    extra = data.get("extra")
    bub_extra = extra.get("bub") if isinstance(extra, dict) else None
    if isinstance(bub_extra, dict) and isinstance(bub_extra.get("message"), dict):
        message.update(bub_extra["message"])
    calls = _normalize_tool_calls(data.get("tool_calls"))
    if calls:
        message["tool_calls"] = calls
    messages.append(message)

    observation = data.get("observation")
    results = observation.get("results") if isinstance(observation, dict) else None
    if not isinstance(results, list):
        return
    for index, result in enumerate(results):
        messages.append(_build_tool_result_message(result, calls, index))


def _build_tool_result_message(
    result: object,
    pending_calls: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"role": "tool", "content": _render_tool_result(result)}
    message: dict[str, Any] = {"role": "tool", "content": _render_tool_result(result.get("content", ""))}
    source_call_id = result.get("source_call_id")
    if isinstance(source_call_id, str) and source_call_id:
        message["tool_call_id"] = source_call_id
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
        if not isinstance(item, dict):
            continue
        call_id = item.get("tool_call_id")
        name = item.get("function_name")
        arguments = item.get("arguments")
        calls.append({
            "id": str(call_id or ""),
            "type": "function",
            "function": {
                "name": str(name or "unknown"),
                "arguments": json.dumps(arguments if isinstance(arguments, dict) else {}, ensure_ascii=False),
            },
        })
    return calls


def _provider_content(value: object) -> object:
    """Map ATIF text/image content to the model provider message vocabulary."""

    if not isinstance(value, list):
        return value
    parts: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append({"type": "text", "text": part["text"]})
            continue
        source = part.get("source")
        path = source.get("path") if isinstance(source, dict) else None
        if part.get("type") == "image" and isinstance(path, str):
            parts.append({"type": "image_url", "image_url": {"url": path}})
    return parts


def _render_tool_result(result: object) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except TypeError:
        return str(result)
