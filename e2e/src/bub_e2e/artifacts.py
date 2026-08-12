"""Read evidence emitted by Harbor and installed Bub plugins."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ArtifactError(ValueError):
    """Raised when a required artifact is malformed."""


@dataclass(frozen=True, slots=True)
class TapeSummary:
    snapshots: int
    tapes: int
    entries: int
    segments: int
    turns: int
    steps: int
    model_calls: int
    tool_calls: int
    tool_results: int
    tool_errors: int
    anchors: int
    handoffs: int
    terminal_runs: int
    total_tokens: int | None
    cached_tokens: int | None

    @property
    def tool_pairs(self) -> int:
        return min(self.tool_calls, self.tool_results)


def summarize_tapes(output_dir: Path) -> TapeSummary:
    manifests = sorted(output_dir.rglob("tape-dataset/manifest.json"))
    raw_snapshots = _raw_snapshots(output_dir) if not manifests else []
    if not manifests and not raw_snapshots:
        return TapeSummary(
            snapshots=0,
            tapes=0,
            entries=0,
            segments=0,
            turns=0,
            steps=0,
            model_calls=0,
            tool_calls=0,
            tool_results=0,
            tool_errors=0,
            anchors=0,
            handoffs=0,
            terminal_runs=0,
            total_tokens=None,
            cached_tokens=None,
        )

    if manifests:
        snapshot = max(manifests, key=lambda path: _manifest_count(path, "entry_count"))
        entries = _entries_for_export(snapshot.parent)
        snapshots = len(manifests)
        segments = _manifest_count(snapshot, "segment_count")
    else:
        entries = max(raw_snapshots, key=len)
        snapshots = len(raw_snapshots)
        segments = _segment_count(entries)
    tape_names = {str(row.get("tape", "")) for row in entries if row.get("tape")}
    tape_entries = [row["entry"] for row in entries]

    tool_calls = sum(_payload_list_size(entry, "calls") for entry in tape_entries if entry.get("kind") == "tool_call")
    tool_results = sum(
        _payload_list_size(entry, "results") for entry in tape_entries if entry.get("kind") == "tool_result"
    )
    run_events = list(_events_named(tape_entries, "run"))
    usages: list[dict[str, Any]] = []
    for data in run_events:
        usage = data.get("usage")
        if isinstance(usage, dict):
            usages.append(usage)
    total_tokens = _sum_optional_int(usages, "total_tokens")
    cached_tokens = _sum_nested_optional_int(usages, "prompt_tokens_details", "cached_tokens")

    return TapeSummary(
        snapshots=snapshots,
        tapes=len(tape_names),
        entries=len(tape_entries),
        segments=segments,
        turns=sum(1 for _ in _events_named(tape_entries, "loop.start")),
        steps=sum(1 for _ in _events_named(tape_entries, "loop.step")),
        model_calls=len(run_events),
        tool_calls=tool_calls,
        tool_results=tool_results,
        tool_errors=sum(1 for entry in tape_entries if entry.get("kind") == "error"),
        anchors=sum(1 for entry in tape_entries if entry.get("kind") == "anchor"),
        handoffs=sum(1 for _ in _events_named(tape_entries, "handoff")),
        terminal_runs=sum(data.get("status") in {"ok", "error"} for data in run_events),
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
    )


def read_hooks(output_dir: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in output_dir.rglob("bub-hooks.txt"))


def required_native_artifacts(output_dir: Path) -> set[str]:
    required_names = {"acp-summary.json", "acp-events.jsonl", "trajectory.json"}
    found = {path.name for path in output_dir.rglob("*") if path.is_file() and path.name in required_names}
    return required_names - found


def read_phoenix_traces(output_dir: Path) -> list[dict[str, Any]]:
    path = output_dir / "phoenix" / "traces.json"
    if not path.is_file():
        return []
    payload = _read_json(path)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ArtifactError(f"Phoenix trace artifact has no data list: {path}")
    return [trace for trace in data if isinstance(trace, dict)]


def _entries_for_export(export_dir: Path) -> list[dict[str, Any]]:
    entries_path = export_dir / "entries.jsonl"
    if not entries_path.is_file():
        raise ArtifactError(f"Tape export is missing entries.jsonl: {export_dir}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(entries_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"Invalid tape entry at {entries_path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("entry"), dict):
            raise ArtifactError(f"Invalid tape entry shape at {entries_path}:{line_number}")
        rows.append(row)
    return rows


def _raw_snapshots(output_dir: Path) -> list[list[dict[str, Any]]]:
    snapshots: list[list[dict[str, Any]]] = []
    for directory in sorted(path for path in output_dir.rglob("raw-tapes") if path.is_dir()):
        rows: list[dict[str, Any]] = []
        for tape_path in sorted(directory.glob("*.jsonl")):
            for line_number, line in enumerate(tape_path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ArtifactError(f"Invalid tape entry at {tape_path}:{line_number}: {exc}") from exc
                if not isinstance(entry, dict):
                    raise ArtifactError(f"Invalid tape entry shape at {tape_path}:{line_number}")
                rows.append({"tape": tape_path.stem, "entry": entry})
        if rows:
            snapshots.append(rows)
    return snapshots


def _segment_count(entries: list[dict[str, Any]]) -> int:
    by_tape: dict[str, list[dict[str, Any]]] = {}
    for row in entries:
        entry = row.get("entry")
        if isinstance(entry, dict):
            by_tape.setdefault(str(row.get("tape", "")), []).append(entry)
    return sum(max(1, sum(entry.get("kind") == "anchor" for entry in tape)) for tape in by_tape.values())


def _events_named(entries: Iterable[dict[str, Any]], name: str) -> Iterable[dict[str, Any]]:
    for entry in entries:
        if entry.get("kind") != "event":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("name") != name:
            continue
        data = payload.get("data")
        yield data if isinstance(data, dict) else {}


def _payload_list_size(entry: dict[str, Any], name: str) -> int:
    payload = entry.get("payload")
    values = payload.get(name) if isinstance(payload, dict) else None
    return len(values) if isinstance(values, list) else 0


def _manifest_count(path: Path, name: str) -> int:
    manifest = _read_json(path)
    if not isinstance(manifest, dict):
        raise ArtifactError(f"Tape manifest is not a JSON object: {path}")
    value = manifest.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"Invalid JSON artifact {path}: {exc}") from exc


def _sum_optional_int(values: Iterable[dict[str, Any]], key: str) -> int | None:
    numbers = [value[key] for value in values if isinstance(value.get(key), int) and not isinstance(value[key], bool)]
    return sum(numbers) if numbers else None


def _sum_nested_optional_int(values: Iterable[dict[str, Any]], outer: str, inner: str) -> int | None:
    numbers: list[int] = []
    for value in values:
        details = value.get(outer)
        number = details.get(inner) if isinstance(details, dict) else None
        if isinstance(number, int) and not isinstance(number, bool):
            numbers.append(number)
    return sum(numbers) if numbers else None
