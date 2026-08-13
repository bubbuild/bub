"""Execute Bub cases through Harbor and preserve externally observable evidence."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import urlopen
from uuid import uuid4

from harbor.job import Job
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import DatasetConfig, JobConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, ResourceMode, ServiceVolumeConfig

from .evaluate import evaluate_run, load_run
from .models import (
    CaseManifest,
    HarborObservation,
    RunArtifact,
    RunEnvironment,
    fingerprint,
)
from .report import write_reports, write_suite_report
from .settings import HarnessSettings

FORWARDED_ENVIRONMENT = (
    "ANTHROPIC_API_KEY",
    "BUB_API_BASE",
    "BUB_API_KEY",
    "BUB_CLIENT_ARGS",
    "BUB_MODEL",
    "DEEPSEEK_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_APP_TITLE",
    "OPENROUTER_APP_URL",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


async def run_cases(
    cases: tuple[CaseManifest, ...],
    *,
    output_dir: Path,
    settings: HarnessSettings,
) -> bool:
    results = []
    for case in cases:
        case_dir = output_dir / case.id
        run = await run_case(case, output_dir=case_dir, settings=settings)
        report = evaluate_run(run, case_dir)
        write_reports(run, report, case_dir)
        results.append((run, report))
    write_suite_report(results, output_dir)
    return all(report.accepted for _, report in results)


async def run_case(case: CaseManifest, *, output_dir: Path, settings: HarnessSettings) -> RunArtifact:
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)
    run_id = f"{case.id}-{uuid4().hex[:12]}"
    harbor = HarborObservation()

    try:
        job = await Job.create(_job_config(case, run_id, output_dir, settings))
        result = await job.run()
        harbor = _harbor_observation(result, output_dir)
    except Exception as exc:
        harbor = HarborObservation(exception_type=type(exc).__name__, exception_message=str(exc))

    finished_at = datetime.now(UTC)
    if case.evaluation.require_otel_trace:
        await _collect_phoenix_traces(output_dir, started_at, finished_at, settings)

    artifact_roots = [output_dir / harbor.trial_path] if harbor.trial_path else []
    if (output_dir / "phoenix").is_dir():
        artifact_roots.append(output_dir / "phoenix")
    artifact_paths = sorted(path for root in artifact_roots for path in root.rglob("*") if path.is_file())
    run = RunArtifact(
        case=case,
        environment=RunEnvironment(
            harness_commit=settings.commit_id(),
            started_at=started_at,
            finished_at=finished_at,
        ),
        harbor=harbor,
        artifacts=tuple(fingerprint(path, relative_to=output_dir) for path in artifact_paths),
    )
    (output_dir / "run.json").write_text(run.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8")
    return run


def rescore(run_path: Path, *, output_dir: Path | None = None) -> bool:
    resolved = run_path / "run.json" if run_path.is_dir() else run_path
    run = load_run(resolved)
    source_dir = resolved.parent
    report_dir = output_dir or source_dir
    report = evaluate_run(run, source_dir)
    write_reports(run, report, report_dir)
    return report.accepted


def _job_config(
    case: CaseManifest,
    run_id: str,
    output_dir: Path,
    settings: HarnessSettings,
) -> JobConfig:
    repository = settings.repository_path()
    mounts: list[ServiceVolumeConfig] = []
    if case.agent.model_source == "codex-oauth":
        auth_path = settings.codex_home.expanduser().resolve() / "auth.json"
        if not auth_path.is_file():
            raise FileNotFoundError(f"Codex OAuth case requires {auth_path}")
        mounts.append({
            "type": "bind",
            "source": str(auth_path),
            "target": "/run/bub-e2e/codex-auth.json",
            "read_only": True,
            "bind": {"create_host_path": False},
        })

    return JobConfig(
        job_name=run_id,
        jobs_dir=output_dir / "harbor-jobs",
        n_attempts=1,
        n_concurrent_trials=1,
        quiet=True,
        environment=EnvironmentConfig(
            type=EnvironmentType.DOCKER,
            delete=True,
            cpu_enforcement_policy=ResourceMode.IGNORE,
            memory_enforcement_policy=ResourceMode.IGNORE,
            extra_docker_compose=[repository / "e2e" / "harbor-task-overlay.yaml"],
            mounts=mounts,
        ),
        agents=[
            AgentConfig(
                import_path="bub_e2e.harbor_agent:BubAcpAgent",
                override_timeout_sec=case.agent.budgets.timeout_seconds,
                override_setup_timeout_sec=case.agent.budgets.setup_timeout_seconds,
                kwargs={
                    "bub": case.agent.bub.model_dump(mode="json"),
                    "plugins": [plugin.model_dump(mode="json") for plugin in case.agent.plugins],
                },
                env=_agent_environment(case),
            )
        ],
        datasets=[_dataset_config(case, repository)],
    )


def _dataset_config(case: CaseManifest, repository: Path) -> DatasetConfig:
    dataset = case.dataset
    if dataset.path is not None:
        return DatasetConfig(path=repository / dataset.path, task_names=[dataset.task_id])
    if dataset.name is not None and "/" in dataset.name:
        return DatasetConfig(name=dataset.name, ref=dataset.version, task_names=[dataset.task_id])
    return DatasetConfig(name=dataset.name, version=dataset.version, task_names=[dataset.task_id])


def _agent_environment(case: CaseManifest) -> dict[str, str]:
    budgets = case.agent.budgets
    environment = {name: value for name in FORWARDED_ENVIRONMENT if (value := os.getenv(name)) is not None}
    environment.update({
        "BUB_FALLBACK_MODELS": "null",
        "BUB_HOME": "/installed-agent/bub-home",
        "BUB_MAX_STEPS": str(budgets.max_agent_steps),
        "BUB_MAX_TOKENS": str(budgets.max_tokens_per_call),
        "BUB_MODEL_TIMEOUT_SECONDS": str(budgets.timeout_seconds),
        "BUB_PROJECT": "/installed-agent/bub-project",
        "CODEX_HOME": "/installed-agent/codex",
    })
    environment["BUB_MODEL"] = case.agent.model
    environment.update(case.agent.environment)
    return environment


def _harbor_observation(result: Any, output_dir: Path) -> HarborObservation:
    if not result.trial_results:
        return HarborObservation(job_id=str(result.id))
    trial = result.trial_results[0]
    rewards = trial.verifier_result.rewards if trial.verifier_result is not None else {}
    exception = trial.exception_info
    trial_path = _relative_trial_path(trial.trial_uri, output_dir)
    return HarborObservation(
        job_id=str(result.id),
        trial_name=trial.trial_name,
        trial_uri=trial.trial_uri,
        trial_path=trial_path,
        task_checksum=trial.task_checksum,
        rewards=rewards or {},
        exception_type=None if exception is None else exception.exception_type,
        exception_message=None if exception is None else exception.exception_message,
    )


def _relative_trial_path(trial_uri: str, output_dir: Path) -> str | None:
    parsed = urlparse(trial_uri)
    if parsed.scheme != "file":
        return None
    try:
        return Path(unquote(parsed.path)).relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        return None


async def _collect_phoenix_traces(
    output_dir: Path,
    started_at: datetime,
    finished_at: datetime,
    settings: HarnessSettings,
) -> None:
    query = urlencode({
        "include_spans": "true",
        "start_time": started_at.isoformat(),
        "end_time": finished_at.isoformat(),
    })
    project = settings.phoenix_project.replace("/", "%2F")
    url = f"{settings.phoenix_url.rstrip('/')}/v1/projects/{project}/traces?{query}"
    phoenix_dir = output_dir / "phoenix"
    phoenix_dir.mkdir(parents=True, exist_ok=True)
    error: Exception | None = None
    for _ in range(5):
        try:
            payload = await asyncio.to_thread(_get_json, url)
        except (OSError, ValueError, URLError) as exc:
            error = exc
            await asyncio.sleep(1)
        else:
            (phoenix_dir / "traces.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return
    (phoenix_dir / "request-error.txt").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")


def _get_json(url: str) -> Any:
    with urlopen(url, timeout=10) as response:  # noqa: S310
        return json.load(response)
