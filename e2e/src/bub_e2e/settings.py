"""Environment settings for the external harness."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


class HarnessSettings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore", frozen=True)

    repository: Path = Field(default_factory=repository_root, validation_alias="BUB_E2E_REPOSITORY")
    output: Path = Field(default=Path(".bub-e2e/run"), validation_alias="BUB_E2E_OUTPUT")
    codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex", validation_alias="CODEX_HOME")
    harness_commit: str | None = Field(default=None, validation_alias="GITHUB_SHA")
    phoenix_url: str = Field(default="http://127.0.0.1:6006", validation_alias="BUB_E2E_PHOENIX_URL")
    phoenix_project: str = Field(default="bub-e2e", validation_alias="BUB_E2E_PHOENIX_PROJECT")

    def repository_path(self) -> Path:
        return self.repository.expanduser().resolve()

    def output_path(self) -> Path:
        return self.output.expanduser().resolve()

    def commit_id(self) -> str:
        if self.harness_commit:
            return self.harness_commit
        git = shutil.which("git")
        if git is None:
            return "unknown"
        completed = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=self.repository_path(),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip() if completed.returncode == 0 else "unknown"
