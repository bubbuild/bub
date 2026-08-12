"""Write machine-readable and human-readable evaluation reports."""

from __future__ import annotations

from pathlib import Path

from .models import EvaluationReport, RunArtifact


def write_reports(run: RunArtifact, report: EvaluationReport, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "eval-report.json").write_text(
        report.model_dump_json(by_alias=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(render_report(run, report), encoding="utf-8")


def render_report(run: RunArtifact, report: EvaluationReport) -> str:
    status = "PASS" if report.accepted else "FAIL"
    lines = [
        "# Bub end-to-end evaluation",
        "",
        f"- Case: `{run.case.id}`",
        f"- Task: `{run.case.dataset.task_id}`",
        f"- Bub: `{report.labels['bub_revision']}`",
        f"- Result: **{status}**",
        "",
        "## Assertions",
        "",
    ]
    for name, result in report.assertions.items():
        assertion_status = "PASS" if result.value is True else "FAIL"
        reason = f" — {result.reason}" if result.reason else ""
        lines.append(f"- `{assertion_status}` `{name}`{reason}")

    lines.extend(("", "## Metrics", ""))
    lines.extend(f"- `{name}`: `{value}`" for name, value in report.metrics.items())
    lines.append("")
    return "\n".join(lines)
