from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import bub.builtin.auth as auth
import bub.builtin.cli as cli
from bub.framework import BubFramework


def _create_app() -> object:
    framework = BubFramework()
    framework.load_hooks()
    return framework.create_cli_app()


def test_login_openai_runs_oauth_flow_and_prints_usage_hint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_login_openai_codex_oauth(**kwargs: object) -> auth.OpenAICodexOAuthTokens:
        captured.update(kwargs)
        prompt_for_redirect = kwargs["prompt_for_redirect"]
        assert callable(prompt_for_redirect)
        callback = prompt_for_redirect("https://auth.openai.com/authorize")
        assert callback == "http://localhost:1455/auth/callback?code=test"
        return auth.OpenAICodexOAuthTokens(
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            expires_at=123,
            account_id="acct_123",
        )

    monkeypatch.setattr(auth, "login_openai_codex_oauth", fake_login_openai_codex_oauth)
    monkeypatch.setattr(auth.typer, "prompt", lambda message: "http://localhost:1455/auth/callback?code=test")

    result = CliRunner().invoke(
        _create_app(),
        ["login", "openai", "--manual", "--no-browser", "--codex-home", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert captured["codex_home"] == tmp_path
    assert captured["open_browser"] is False
    assert captured["redirect_uri"] == auth.DEFAULT_CODEX_REDIRECT_URI
    assert captured["timeout_seconds"] == 300.0
    assert "login: ok" in result.stdout
    assert "account_id: acct_123" in result.stdout
    assert f"auth_file: {tmp_path / 'auth.json'}" in result.stdout
    assert "BUB_MODEL=openai:gpt-5-codex" in result.stdout


def test_login_openai_surfaces_oauth_errors(monkeypatch) -> None:
    def fake_login_openai_codex_oauth(**kwargs: object) -> auth.OpenAICodexOAuthTokens:
        raise auth.CodexOAuthLoginError("bad redirect")

    monkeypatch.setattr(auth, "login_openai_codex_oauth", fake_login_openai_codex_oauth)

    result = CliRunner().invoke(_create_app(), ["login", "openai", "--manual"])

    assert result.exit_code == 1
    assert "Codex login failed: bad redirect" in result.stderr


def test_login_rejects_unsupported_provider() -> None:
    result = CliRunner().invoke(_create_app(), ["login", "anthropic"])

    assert result.exit_code == 2
    assert "No such command 'anthropic'" in result.stderr


def test_build_bub_requirement_uses_direct_url_json(monkeypatch) -> None:
    class FakeDistribution:
        version = "0.3.4"
        name = "bub"

        def read_text(self, filename: str) -> str:
            assert filename == "direct_url.json"
            return json.dumps({
                "url": "https://github.com/bubbuild/bub.git",
                "vcs_info": {"vcs": "git", "requested_revision": "main"},
                "subdirectory": "python",
            })

    monkeypatch.setattr(cli.metadata, "distribution", lambda name: FakeDistribution())

    assert cli._build_bub_requirement() == ["git+https://github.com/bubbuild/bub.git@main#subdirectory=python"]


def test_build_bub_requirement_falls_back_to_installed_version(monkeypatch) -> None:
    class FakeDistribution:
        version = "0.3.4"
        name = "bub"

        def read_text(self, filename: str) -> None:
            assert filename == "direct_url.json"
            return None

    monkeypatch.setattr(cli.metadata, "distribution", lambda name: FakeDistribution())

    assert cli._build_bub_requirement() == ["bub"]


def test_build_bub_requirement_uses_local_path_for_file_dist(monkeypatch) -> None:
    class FakeDistribution:
        name = "bub"

        def read_text(self, filename: str) -> str:
            assert filename == "direct_url.json"
            return json.dumps({"url": "file:///tmp/worktrees/bub"})

    monkeypatch.setattr(cli.metadata, "distribution", lambda name: FakeDistribution())

    assert cli._build_bub_requirement() == ["/tmp/worktrees/bub"]  # noqa: S108


def test_build_bub_requirement_marks_editable_local_dist(monkeypatch) -> None:
    class FakeDistribution:
        name = "bub"

        def read_text(self, filename: str) -> str:
            assert filename == "direct_url.json"
            return json.dumps({
                "url": "file:///tmp/worktrees/bub",
                "dir_info": {"editable": True},
            })

    monkeypatch.setattr(cli.metadata, "distribution", lambda name: FakeDistribution())

    assert cli._build_bub_requirement() == ["--editable", "/tmp/worktrees/bub"]  # noqa: S108


def test_ensure_project_initializes_project_and_adds_bub_dependency(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "managed-project"
    project.mkdir()
    captured: list[tuple[tuple[str, ...], Path]] = []

    monkeypatch.setattr(cli, "_build_bub_requirement", lambda: ["--editable", "/tmp/bub"])  # noqa: S108
    monkeypatch.setattr(cli, "_uv", lambda *args, cwd: captured.append((args, cwd)))

    cli._ensure_project(project)

    assert captured == [
        (("init", "--bare", "--name", "bub-project", "--app"), project),
        (("add", "--active", "--no-sync", "--editable", "/tmp/bub"), project),  # noqa: S108
    ]
