"""Archive tapes to JSONL files before they are reset."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bub.core.sidecars import sidecar_tape_name
from bub.core.store import TapeQuery
from bub.core.tape import Tape


def archive_dir() -> Path:
    """Bub's archive directory, ``bub.home / "tapes"``."""
    import bub

    return bub.home / "tapes"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


async def _write_archive(tape: Tape, tape_name: str, directory: Path, stamp: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tape_name}.jsonl.{stamp}.bak"
    with path.open("w", encoding="utf-8") as f:
        for entry in await tape.store.fetch_all(TapeQuery(tape=tape_name, store=tape.store)):
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
    return path


async def archive_tape(tape: Tape, directory: Path, *, stamp: str | None = None) -> Path:
    """Write the tape's entries to ``{directory}/{tape name}.jsonl.{stamp}.bak`` and return the path."""
    return await _write_archive(tape, tape.name, directory, stamp or _stamp())


async def archive_sidecar(
    tape: Tape, name: str, directory: Path, *, reason: str = "manual", stamp: str | None = None
) -> tuple[Path | None, dict[str, Any]]:
    """Archive one mounted sidecar of ``tape``; return the archive path and ``sidecar.archive`` event data.

    A failed archive returns no path and error data instead of raising.
    """
    if tape.get_sidecar(name) is None:
        raise KeyError(f"tape sidecar {name!r} is not mounted")
    data: dict[str, Any] = {"sidecar": name, "reason": reason}
    try:
        path = await _write_archive(tape, sidecar_tape_name(tape.name, name), directory, stamp or _stamp())
    except Exception as exc:
        return None, {**data, "status": "error", "error": str(exc)}
    return path, {**data, "status": "ok", "archive": str(path)}


async def reset_tape(tape: Tape, *, archive_to: Path | None = None) -> str:
    """Reset ``tape``, first archiving it and its sidecars to ``archive_to`` when given.

    A sidecar whose archive fails is kept rather than reset; archive outcomes are recorded
    as ``sidecar.archive`` events after the reset.
    """
    if archive_to is None:
        await tape.reset()
        return "ok"
    stamp = _stamp()
    path = await archive_tape(tape, archive_to, stamp=stamp)
    archives = [
        await archive_sidecar(tape, sidecar.name, archive_to, reason="tape.reset", stamp=stamp)
        for sidecar in tape.sidecars
    ]
    failed = [data["sidecar"] for archived, data in archives if archived is None]
    await tape.reset(state={"archived": str(path)}, keep_sidecars=failed)
    for _, data in archives:
        await tape.append_event("sidecar.archive", data, context=False)
        if data["status"] == "error":
            skipped = {
                "sidecar": data["sidecar"],
                "status": "skipped",
                "reason": "tape.reset",
                "cause": "archive_failed",
            }
            await tape.append_event("sidecar.reset", skipped, context=False)
    return f"Archived: {path}"
