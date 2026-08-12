"""Offline evaluation over Harbor, tape, and optional Phoenix artifacts."""

from __future__ import annotations

from pathlib import Path

from .artifacts import ArtifactError, read_hooks, read_phoenix_traces, required_native_artifacts, summarize_tapes
from .models import EvaluationReport, EvaluationValue, RunArtifact


def evaluate_run(run: RunArtifact, output_dir: Path) -> EvaluationReport:
    evidence_dir = output_dir / run.harbor.trial_path if run.harbor.trial_path else output_dir
    try:
        tape = summarize_tapes(evidence_dir)
        tape_error: str | None = None
    except ArtifactError as exc:
        tape = None
        tape_error = str(exc)

    hooks = read_hooks(evidence_dir)
    missing_native = required_native_artifacts(evidence_dir)
    expected_plugins = {_expected_hook_name(plugin.name) for plugin in run.case.agent.plugins}
    missing_plugins = sorted(name for name in expected_plugins if name not in hooks)
    rewards = [float(reward) for reward in run.harbor.rewards.values()]
    observed_reward = max(rewards, default=0.0)
    elapsed_seconds = (run.environment.finished_at - run.environment.started_at).total_seconds()
    budgets = run.case.agent.budgets
    evaluation = run.case.evaluation

    has_tape = tape is not None and tape.snapshots > 0
    token_budget_passed = budgets.max_total_tokens is None or (
        tape is not None and tape.total_tokens is not None and tape.total_tokens <= budgets.max_total_tokens
    )
    try:
        traces = read_phoenix_traces(output_dir) if evaluation.require_otel_trace else []
        trace_error: str | None = None
    except ArtifactError as exc:
        traces = []
        trace_error = str(exc)
    span_names = {
        str(span.get("name", "")) for trace in traces for span in trace.get("spans", []) if isinstance(span, dict)
    }

    assertions = {
        "task_provenance_matches": EvaluationValue(
            value=run.harbor.task_checksum == run.case.dataset.checksum,
            reason=f"Expected {run.case.dataset.checksum!r}; observed {run.harbor.task_checksum!r}.",
        ),
        "native_acp_evidence_exists": EvaluationValue(
            value=not missing_native,
            reason="All required ACP artifacts exist."
            if not missing_native
            else f"Missing {sorted(missing_native)!r}.",
        ),
        "plugins_discovered": EvaluationValue(
            value=not missing_plugins,
            reason="All declared plugins were discovered." if not missing_plugins else f"Missing {missing_plugins!r}.",
        ),
        "agent_completed": EvaluationValue(
            value=run.harbor.exception_type is None,
            reason=run.harbor.exception_message,
        ),
        "native_verifier_passed": EvaluationValue(
            value=observed_reward >= evaluation.required_reward,
            reason=f"Observed {observed_reward}; required {evaluation.required_reward}.",
        ),
        "tape_evidence_valid": EvaluationValue(
            value=has_tape,
            reason=tape_error or (None if has_tape else "No canonical tape evidence was found."),
        ),
        "tape_run_completed": EvaluationValue(
            value=bool(
                tape and tape.turns >= evaluation.minimum_turns and tape.terminal_turns >= evaluation.minimum_turns
            ),
            reason=(
                f"Observed {tape.turns if tape else 0} turns and "
                f"{tape.terminal_turns if tape else 0} terminal turn records; required {evaluation.minimum_turns}."
            ),
        ),
        "tool_pairs_recorded": EvaluationValue(
            value=bool(tape and tape.tool_pairs >= evaluation.minimum_tool_pairs),
            reason=f"Observed {tape.tool_pairs if tape else 0}; required {evaluation.minimum_tool_pairs}.",
        ),
        "anchor_segments_recorded": EvaluationValue(
            value=bool(tape and tape.anchors > 0 and tape.segments > 0),
            reason=f"Observed {tape.anchors if tape else 0} anchors and {tape.segments if tape else 0} segments.",
        ),
        "step_budget_passed": EvaluationValue(
            value=bool(tape and tape.steps <= budgets.max_agent_steps),
            reason=f"Observed {tape.steps if tape else 0}; maximum {budgets.max_agent_steps}.",
        ),
        "token_budget_passed": EvaluationValue(
            value=token_budget_passed,
            reason=_token_budget_reason(tape.total_tokens if tape else None, budgets.max_total_tokens),
        ),
        "time_budget_passed": EvaluationValue(
            value=elapsed_seconds <= budgets.timeout_seconds,
            reason=f"Observed {elapsed_seconds:.3f}s; maximum {budgets.timeout_seconds}s.",
        ),
    }
    if evaluation.require_otel_trace:
        has_agent_span = "invoke_agent bub" in span_names
        has_step_span = "bub.agent.step" in span_names
        has_model_span = any(name == "chat" or name.startswith("chat ") for name in span_names)
        has_tool_span = any(name.startswith("execute_tool ") for name in span_names)
        assertions["otel_trace_recorded"] = EvaluationValue(
            value=bool(traces) and has_agent_span and has_step_span and has_model_span and has_tool_span,
            reason=trace_error or f"Observed {len(traces)} traces with spans {sorted(span_names)!r}.",
        )

    return EvaluationReport(
        case_id=run.case.id,
        assertions=assertions,
        metrics={
            "wall_time_seconds": elapsed_seconds,
            "harbor_reward": observed_reward,
            "tape_snapshots": tape.snapshots if tape else 0,
            "tapes": tape.tapes if tape else 0,
            "tape_entries": tape.entries if tape else 0,
            "tape_segments": tape.segments if tape else 0,
            "agent_turns": tape.turns if tape else 0,
            "agent_steps": tape.steps if tape else 0,
            "model_calls": tape.model_calls if tape else 0,
            "tool_calls": tape.tool_calls if tape else 0,
            "tool_errors": tape.tool_errors if tape else 0,
            "anchors": tape.anchors if tape else 0,
            "handoffs": tape.handoffs if tape else 0,
            "total_tokens": tape.total_tokens if tape else None,
            "cached_tokens": tape.cached_tokens if tape else None,
            "otel_traces": len(traces),
        },
        labels={
            "bub_revision": run.case.agent.bub.version or run.case.agent.bub.commit or "unknown",
            "task_id": run.case.dataset.task_id,
            "task_outcome": _task_outcome(run),
        },
    )


def _expected_hook_name(package_name: str) -> str:
    return package_name.removeprefix("bub-")


def _token_budget_reason(observed: int | None, maximum: int | None) -> str:
    if maximum is None:
        return "No total-token budget was declared."
    if observed is None:
        return "A total-token budget was declared, but the provider reported no usage."
    return f"Observed {observed}; maximum {maximum}."


def _task_outcome(run: RunArtifact) -> str:
    if run.harbor.exception_type:
        return f"error:{run.harbor.exception_type}"
    rewards = [float(reward) for reward in run.harbor.rewards.values()]
    return "passed" if max(rewards, default=0.0) >= run.case.evaluation.required_reward else "not_passed"


def load_run(path: Path) -> RunArtifact:
    return RunArtifact.model_validate_json(path.read_text(encoding="utf-8"))
