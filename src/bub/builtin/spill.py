"""Spill oversized tool outputs into a dedicated spill tape.

Large tool results are written once to a ``spill`` tape (the same store as the
session tapes) and the model-facing result is replaced with a short ref: a
handle, a shape sketch, and a bounded preview. The full payload stays in the
tape store — queryable, replayable, and deletable by the user — without ever
entering a model request.

The spill tape is intentionally shared across sessions and has no built-in
cleanup: the user owns retention, exactly like the session tapes themselves.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from bub.tape import AsyncTapeStore, TapeEntry, TapeQuery

SPILL_TAPE = "spill"
"""Name of the tape that stores full tool outputs."""

READ_TOOL_RESULT_NAME = "read_tool_result"
"""Name of the bounded read-back tool. Its own returns are never spilled."""

PREVIEW_CHARS = 600
"""Characters of head+tail preview kept inline in a spill ref."""

MAX_READ_LINES = 1000
"""Hard cap on lines returned by one read_tool_result call."""

MAX_READ_CHARS = 50_000
"""Hard cap on characters returned by one read_tool_result call."""


class SpillStore(Protocol):
    """The only capability spilling needs: append entries."""

    async def append(self, tape: str, entry: TapeEntry) -> None: ...


def needs_spill(output: str, *, threshold: int) -> bool:
    """Decide whether one stringified tool result should be spilled.

    ``threshold`` is measured in estimated tokens (4 chars per token), matching
    the heuristic used by pydantic-ai-harness. ``0`` disables spilling.
    """
    if threshold <= 0:
        return False
    return len(output) // 4 >= threshold


def handle_key(run_id: str, tool: str) -> str:
    """Build a unique handle for one spill (the spill tape's lookup key)."""
    return f"{run_id}/{tool}.{uuid.uuid4().hex[:8]}"


def spill_ref(handle: str, output: str) -> str:
    """Build the model-visible ref that replaces an oversized tool result."""
    lines = output.splitlines()
    total = len(output)
    body = _head_tail_preview(output)
    return (
        f"[tool output spilled: {total:,} chars in {len(lines):,} lines; "
        f"handle: {handle}]\n"
        f"[read it back: read_tool_result(handle={handle!r}, offset=0, limit=200, "
        f"from_end=False, pattern=None)]\n"
        f"{body}"
    )


def _head_tail_preview(text: str, preview_chars: int = PREVIEW_CHARS) -> str:
    if len(text) <= preview_chars:
        return text
    head = preview_chars // 2
    tail = preview_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{omitted:,} chars omitted]...\n{text[-tail:]}"


async def maybe_spill(
    *,
    tool: str,
    run_id: str | None,
    result: Any,
    store: SpillStore | None,
) -> Any:
    """Rewrite an oversized string tool result into a spill ref; no-op otherwise.

    Every "can't spill" case — non-string result, the read-back tool itself, a
    missing store, spilling disabled, or a failed write — keeps the original
    result. Errors are never spilled (the model needs full error text to
    recover) and spilling must never fail a turn.
    """
    if not isinstance(result, str):
        return result
    if tool == READ_TOOL_RESULT_NAME:
        return result
    if store is None:
        return result

    from bub.builtin.settings import load_settings

    threshold = load_settings().tool_spill_threshold
    if not needs_spill(result, threshold=threshold):
        return result

    handle = handle_key(run_id or "run", tool)
    entry = TapeEntry.tool_result([result], spill_handle=handle)
    try:
        await store.append(SPILL_TAPE, entry)
    except Exception:
        return result
    return spill_ref(handle, result)


async def read_spilled(*, store: AsyncTapeStore, handle: str) -> str | None:
    """Return the full spilled payload for ``handle``, or None when unknown."""
    key = handle.strip().lstrip("/")
    query = TapeQuery(tape=SPILL_TAPE, store=store).kinds("tool_result")
    entries = await store.fetch_all(query)
    for entry in reversed(list(entries)):  # newest wins; handles are unique per spill
        if entry.meta.get("spill_handle") == key:
            payload = entry.payload.get("results")
            if isinstance(payload, list) and payload:
                value = payload[0]
                return value if isinstance(value, str) else str(value)
    return None


def read_slice(output: str, *, offset: int, limit: int, from_end: bool, pattern: str | None) -> str:
    """Slice a spilled payload with hard bounds; ``pattern`` is a literal substring."""
    lines = output.splitlines()
    if pattern is not None:
        lines = [line for line in lines if pattern in line]

    total = len(lines)
    if from_end:
        end = max(0, total - offset)
        window = lines[max(0, end - limit) : end]
    else:
        window = lines[offset : offset + limit]

    body = "\n".join(window)
    capped = ""
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS]
        capped = ", output capped"
    header = f"[handle: {total:,} matching line(s); showing {len(window)}{capped}]"
    return f"{header}\n{body}" if body else header
