"""Write machine-readable and human-readable evaluation reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from .models import EvaluationReport, RunArtifact


class CaseSummary(TypedDict):
    id: str
    categories: list[str]
    accepted: bool
    reward: float
    wall_time_seconds: float
    agent_steps: int
    model_calls: int
    tool_calls: int
    tool_errors: int
    total_tokens: int | None


def write_reports(run: RunArtifact, report: EvaluationReport, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "eval-report.json").write_text(
        report.model_dump_json(by_alias=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(render_report(run, report), encoding="utf-8")


def write_suite_report(
    results: list[tuple[RunArtifact, EvaluationReport]],
    output_dir: Path,
) -> None:
    """Write a compact run index without replacing per-case evidence."""
    cases = [_case_summary(run, report) for run, report in results]
    passed = sum(1 for case in cases if case["accepted"])
    token_values = [case["total_tokens"] for case in cases if case["total_tokens"] is not None]
    payload: dict[str, object] = {
        "schema": "bub.e2e-suite/v1",
        "totals": {
            "cases": len(cases),
            "passed": passed,
            "failed": len(cases) - passed,
            "pass_rate": passed / len(cases) if cases else 0.0,
            "wall_time_seconds": sum(float(case["wall_time_seconds"]) for case in cases),
            "agent_steps": sum(int(case["agent_steps"]) for case in cases),
            "model_calls": sum(int(case["model_calls"]) for case in cases),
            "tool_calls": sum(int(case["tool_calls"]) for case in cases),
            "tool_errors": sum(int(case["tool_errors"]) for case in cases),
            "total_tokens": (
                sum(int(value) for value in token_values) if cases and len(token_values) == len(cases) else None
            ),
            "cases_with_token_usage": len(token_values),
        },
        "cases": cases,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (output_dir / "summary.md").write_text(_render_suite_report(payload), encoding="utf-8")


def _case_summary(run: RunArtifact, report: EvaluationReport) -> CaseSummary:
    total_tokens = report.metrics["total_tokens"]
    return CaseSummary(
        id=run.case.id,
        categories=list(run.case.categories),
        accepted=report.accepted,
        reward=_float_metric(report, "harbor_reward"),
        wall_time_seconds=_float_metric(report, "wall_time_seconds"),
        agent_steps=_int_metric(report, "agent_steps"),
        model_calls=_int_metric(report, "model_calls"),
        tool_calls=_int_metric(report, "tool_calls"),
        tool_errors=_int_metric(report, "tool_errors"),
        total_tokens=int(total_tokens) if isinstance(total_tokens, int | float) else None,
    )


def _float_metric(report: EvaluationReport, name: str) -> float:
    value = report.metrics[name]
    return float(value) if isinstance(value, int | float) else 0.0


def _int_metric(report: EvaluationReport, name: str) -> int:
    value = report.metrics[name]
    return int(value) if isinstance(value, int | float) else 0


def _render_suite_report(payload: dict[str, object]) -> str:
    totals = payload["totals"]
    cases = payload["cases"]
    if not isinstance(totals, dict) or not isinstance(cases, list):
        raise TypeError("invalid suite report payload")
    lines = [
        "# Bub end-to-end suite",
        "",
        f"- Cases: `{totals['cases']}`",
        f"- Passed: `{totals['passed']}`",
        f"- Failed: `{totals['failed']}`",
        f"- Pass rate: `{float(totals['pass_rate']):.1%}`",
        "",
        "| Case | Result | Reward | Steps | Tool calls | Tokens |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for case in cases:
        if not isinstance(case, dict):
            continue
        status = "PASS" if case["accepted"] else "FAIL"
        tokens = case["total_tokens"] if case["total_tokens"] is not None else "n/a"
        lines.append(
            f"| `{case['id']}` | {status} | {case['reward']} | {case['agent_steps']} | "
            f"{case['tool_calls']} | {tokens} |"
        )
    lines.append("")
    return "\n".join(lines)


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
