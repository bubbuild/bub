"""Bound tool-result size before it reaches the tape and the next model request.

A single oversized tool result -- e.g. ``grep`` over a minified bundle or source
map under ``node_modules`` -- can blow past the provider's request-body limit and
fail the whole turn with a ``413 Request Entity Too Large``. ``head -50`` is no
protection when individual lines are megabytes long.

We cap each result at a byte budget and spill the full output to a file, so the
agent keeps a bounded inline view *and* the complete output for follow-up
inspection with ``tail`` / ``rg``. The cap is enforced at the result boundary, so
it protects the tape, the trace, the streamed event, and the next request alike.
"""

from __future__ import annotations

import shlex
from pathlib import Path

DEFAULT_MAX_TOOL_RESULT_BYTES = 128 * 1024


def _human_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _spill_to_file(data: bytes, *, run_id: str, index: int, spill_dir: Path) -> Path:
    spill_dir.mkdir(parents=True, exist_ok=True)
    path = spill_dir / f"{run_id}-call-{index}.txt"
    path.write_bytes(data)
    return path


def _truncation_footer(*, original_bytes: int, limit: int, spill_path: Path) -> str:
    quoted = shlex.quote(str(spill_path))
    return (
        f"\n\n[output truncated: original {_human_bytes(original_bytes)} "
        f"exceeded {_human_bytes(limit)} limit]\n"
        f"[full output saved to: {spill_path}]\n"
        f"[hint: inspect the end with `tail -c 4096 {quoted}` "
        f"or search it with `rg <pattern> {quoted}`]"
    )


def cap_tool_result(
    result: object,
    *,
    run_id: str,
    index: int,
    limit: int,
    spill_dir: Path,
) -> object:
    """Bound a single tool result so it cannot blow past the request-body limit.

    Oversized string results are truncated to ``limit`` UTF-8 bytes (the footer is
    counted against the budget, so the returned string stays within ``limit``); the
    full output is written to ``spill_dir`` and the truncated text gains a footer
    pointing the agent at the file. Non-string results, results within budget, and
    non-positive limits pass through unchanged.
    """
    if limit <= 0 or not isinstance(result, str):
        return result
    encoded = result.encode("utf-8")
    if len(encoded) <= limit:
        return result

    spill_path = _spill_to_file(encoded, run_id=run_id, index=index, spill_dir=spill_dir)
    footer = _truncation_footer(original_bytes=len(encoded), limit=limit, spill_path=spill_path)
    head_budget = max(0, limit - len(footer.encode("utf-8")))
    # errors="ignore" drops a partial multi-byte char left at the cut boundary.
    head = encoded[:head_budget].decode("utf-8", errors="ignore")
    return head + footer
