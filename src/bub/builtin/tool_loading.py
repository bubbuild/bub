"""Record native definition additions at their position in a conversation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from bub.tools import Tool


def tool_definition_update(messages: list[dict[str, Any]], tools: list[Tool]) -> dict[str, Any] | None:
    """Append additions; start a new definition prefix when scope or schemas change."""
    declared: dict[str, dict[str, Any]] = {}
    history = [message for message in messages if message.get("type") == "tool_definitions"]
    for message in history:
        if message.get("reset"):
            declared.clear()
        declared.update((item["function"]["name"], item) for item in message["tools"])
    current = {item.name: item.to_schema() for item in tools}
    reset = not history or any(name not in current or current[name] != item for name, item in declared.items())
    added = list(current.values()) if reset else [item for name, item in current.items() if name not in declared]
    if not added and not reset:
        return None
    return {"role": "developer", "type": "tool_definitions", "tools": deepcopy(added), "reset": reset}
