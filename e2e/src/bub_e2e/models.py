"""Validated case, run, and evaluation artifacts."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
COMMIT_PATTERN = r"^[0-9a-f]{7,64}$"


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class BubDistribution(ArtifactModel):
    version: str | None = None
    commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    repository: str = "https://github.com/bubbuild/bub.git"

    @model_validator(mode="after")
    def require_one_revision(self) -> BubDistribution:
        if (self.version is None) == (self.commit is None):
            raise ValueError("Bub requires exactly one of version or commit")
        return self

    def requirement(self) -> str:
        if self.version is not None:
            return f"bub=={self.version}"
        return f"bub @ git+{self.repository}@{self.commit}"


class PluginSpec(ArtifactModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN)
    version: str | None = None
    commit: str | None = Field(default=None, pattern=COMMIT_PATTERN)
    spec: str | None = None

    @model_validator(mode="after")
    def require_one_revision(self) -> PluginSpec:
        revisions = (self.version, self.commit, self.spec)
        if sum(value is not None for value in revisions) != 1:
            raise ValueError(f"Plugin {self.name!r} requires exactly one of version, commit, or spec")
        return self

    def install_spec(self) -> str:
        if self.version is not None:
            return f"{self.name}=={self.version}"
        if self.commit is not None:
            return f"{self.name}@{self.commit}"
        if self.spec is None:
            raise RuntimeError(f"Plugin {self.name!r} has no install specification")
        return self.spec


class DatasetSpec(ArtifactModel):
    path: Path | None = None
    name: str | None = None
    version: str | None = None
    task_id: str = Field(min_length=1)
    checksum: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def require_one_source(self) -> DatasetSpec:
        if (self.path is None) == (self.name is None):
            raise ValueError("A dataset requires exactly one of path or name")
        if self.path is not None and self.version is not None:
            raise ValueError("A local dataset cannot declare a registry version")
        return self


class BudgetSpec(ArtifactModel):
    max_agent_steps: int = Field(default=50, ge=1, le=500)
    max_total_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: int = Field(default=3600, ge=60)
    setup_timeout_seconds: int = Field(default=900, ge=60)
    max_tokens_per_call: int = Field(default=16384, ge=256)


class AgentSpec(ArtifactModel):
    bub: BubDistribution
    plugins: tuple[PluginSpec, ...] = Field(min_length=1)
    model: str | None = None
    model_source: Literal["environment", "codex-oauth"] = "environment"
    environment: dict[str, str] = Field(default_factory=dict)
    budgets: BudgetSpec = Field(default_factory=BudgetSpec)

    @model_validator(mode="after")
    def require_runtime_plugins(self) -> AgentSpec:
        names = {plugin.name for plugin in self.plugins}
        missing = {"bub-acp-server", "tape-dataset-opendal"} - names
        if missing:
            raise ValueError(f"Agent is missing required plugins: {sorted(missing)!r}")
        if self.model_source == "codex-oauth" and self.model is None:
            raise ValueError("Codex OAuth cases require an explicit model")
        return self


class EvaluationSpec(ArtifactModel):
    required_reward: float = 1.0
    require_otel_trace: bool = False
    minimum_turns: int = Field(default=1, ge=1)
    minimum_tool_pairs: int = Field(default=1, ge=0)


class CaseManifest(ArtifactModel):
    schema_: Literal["bub.e2e-case/v1"] = Field(alias="schema")
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    categories: tuple[str, ...] = Field(min_length=1)
    dataset: DatasetSpec
    agent: AgentSpec
    services: tuple[Literal["redis", "phoenix"], ...] = ()
    evaluation: EvaluationSpec = Field(default_factory=EvaluationSpec)

    @model_validator(mode="after")
    def require_plugin_services(self) -> CaseManifest:
        names = {plugin.name for plugin in self.agent.plugins}
        if "bub-tapestore-redis" in names and "redis" not in self.services:
            raise ValueError("Redis tape-store cases must declare the redis service")
        if self.evaluation.require_otel_trace and "phoenix" not in self.services:
            raise ValueError("OTel trace evaluation requires the phoenix service")
        return self


class RunEnvironment(ArtifactModel):
    harness_commit: str
    started_at: datetime
    finished_at: datetime


class HarborObservation(ArtifactModel):
    job_id: str | None = None
    trial_name: str | None = None
    trial_uri: str | None = None
    trial_path: str | None = None
    task_checksum: str | None = None
    rewards: dict[str, float | int] = Field(default_factory=dict)
    exception_type: str | None = None
    exception_message: str | None = None


class FileArtifact(ArtifactModel):
    path: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    bytes: int = Field(ge=0)


class RunArtifact(ArtifactModel):
    schema_: Literal["bub.e2e-run/v1"] = Field(default="bub.e2e-run/v1", alias="schema")
    case: CaseManifest
    environment: RunEnvironment
    harbor: HarborObservation
    artifacts: tuple[FileArtifact, ...] = ()


class EvaluationValue(ArtifactModel):
    value: bool | int | float | str | None
    reason: str | None = None


class EvaluationReport(ArtifactModel):
    schema_: Literal["bub.e2e-evaluation/v1"] = Field(default="bub.e2e-evaluation/v1", alias="schema")
    case_id: str
    assertions: dict[str, EvaluationValue]
    metrics: dict[str, int | float | None]
    labels: dict[str, str]

    @property
    def accepted(self) -> bool:
        return all(result.value is True for result in self.assertions.values())


def load_cases(path: Path) -> tuple[CaseManifest, ...]:
    paths = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    cases = tuple(CaseManifest.model_validate(yaml.safe_load(item.read_text(encoding="utf-8"))) for item in paths)
    if not cases:
        raise ValueError(f"No e2e case manifests found at {path}")
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("E2E case IDs must be unique")
    return cases


def select_cases(
    cases: tuple[CaseManifest, ...],
    *,
    ids: tuple[str, ...] = (),
    categories: tuple[str, ...] = (),
) -> tuple[CaseManifest, ...]:
    requested_ids = _selectors(ids)
    requested_categories = _selectors(categories)
    available_ids = {case.id for case in cases}
    available_categories = {category for case in cases for category in case.categories}
    if missing := requested_ids - available_ids:
        raise ValueError(f"Unknown e2e case IDs: {sorted(missing)!r}")
    if missing := requested_categories - available_categories:
        raise ValueError(f"Unknown e2e categories: {sorted(missing)!r}")
    if not requested_ids and not requested_categories:
        requested_categories = {"acceptance"}
    return tuple(
        case for case in cases if case.id in requested_ids or bool(requested_categories.intersection(case.categories))
    )


def fingerprint(path: Path, *, relative_to: Path) -> FileArtifact:
    content = path.read_bytes()
    return FileArtifact(
        path=path.relative_to(relative_to).as_posix(),
        sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
    )


def _selectors(values: tuple[str, ...]) -> set[str]:
    return {selector.strip() for value in values for selector in value.split(",") if selector.strip()}
