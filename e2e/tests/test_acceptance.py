from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bub_e2e.evaluate import evaluate_run
from bub_e2e.models import HarborObservation, RunArtifact, RunEnvironment, load_cases
from bub_e2e.runner import _dataset_config


def test_complete_upstream_evidence_passes_acceptance(tmp_path: Path) -> None:
    e2e_root = Path(__file__).parents[1]
    case = load_cases(e2e_root / "cases" / "builtin.yaml")[0]
    agent_dir = tmp_path / "harbor-jobs" / "trial" / "agent"
    tape_dir = agent_dir / "tape-dataset"
    tape_dir.mkdir(parents=True)

    for name in ("acp-summary.json", "trajectory.json"):
        (agent_dir / name).write_text("{}\n", encoding="utf-8")
    (agent_dir / "acp-events.jsonl").write_text("{}\n", encoding="utf-8")
    (agent_dir / "bub-hooks.txt").write_text(
        "register_cli_commands: acp-server, tape-dataset-opendal, builtin\n",
        encoding="utf-8",
    )
    (tape_dir / "manifest.json").write_text(
        json.dumps({"tape_count": 1, "entry_count": 9, "segment_count": 1}) + "\n",
        encoding="utf-8",
    )
    entries = [
        _entry("anchor", {"name": "bootstrap"}),
        _event("loop.start", {}),
        _entry("tool_call", {"calls": [{"id": "read"}]}),
        _entry("tool_result", {"results": [{"id": "read", "output": "ok"}]}),
        _event("loop.step", {"status": "ok"}),
        _event("run", {"status": "ok", "usage": {"total_tokens": 100}}),
        _event("loop.start", {}),
        _entry("tool_call", {"calls": [{"id": "write"}]}),
        _entry("tool_result", {"results": [{"id": "write", "output": "ok"}]}),
        _event("loop.step", {"status": "ok"}),
        _event("run", {"status": "ok", "usage": {"total_tokens": 120}}),
    ]
    (tape_dir / "entries.jsonl").write_text(
        "".join(json.dumps({"tape": "agent__session", "entry": entry}) + "\n" for entry in entries),
        encoding="utf-8",
    )

    started_at = datetime.now(UTC)
    run = RunArtifact(
        case=case,
        environment=RunEnvironment(
            harness_commit="test",
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=3),
        ),
        harbor=HarborObservation(task_checksum=case.dataset.checksum, rewards={"task": 1}),
    )

    report = evaluate_run(run, tmp_path)

    assert report.accepted
    assert report.metrics["agent_turns"] == 2
    assert report.metrics["tool_calls"] == 2
    assert report.metrics["total_tokens"] == 220


def test_package_dataset_uses_declared_revision() -> None:
    e2e_root = Path(__file__).parents[1]
    case = load_cases(e2e_root / "cases" / "benchmark-swe-atlas-qna.yaml")[0]

    config = _dataset_config(case, e2e_root.parent)

    assert config.ref == "sha256:0e26bc0313ae2fc6f912b67b928e648c7f20d17d91f765f702a93042ce5be0e4"
    assert config.version is None
    assert config.task_names == ["scale-ai/task-6905333b74f22949d97ba9c8"]


def _entry(kind: str, payload: dict[str, object]) -> dict[str, object]:
    return {"id": 1, "kind": kind, "payload": payload, "meta": {}, "date": "2026-01-01T00:00:00+00:00"}


def _event(name: str, data: dict[str, object]) -> dict[str, object]:
    return _entry("event", {"name": name, "data": data})
