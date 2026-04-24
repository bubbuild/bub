from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import typer
from typer.testing import CliRunner

import bub.builtin.auth as auth
import bub.builtin.cli as cli
import bub.builtin.hook_impl as builtin_hook_impl
import bub.configure as configure
from bub.framework import BubFramework
from bub.hookspecs import hookimpl


class _FakeQuestion:
    def __init__(self, answer: Any) -> None:
        self._answer = answer

    def ask(self) -> Any:
        return self._answer


def _create_app() -> typer.Typer:
    framework = BubFramework()
    framework.load_hooks()
    return framework.create_cli_app()


def _rendered_onboard_banner() -> str:
    return cli.ONBOARD_BANNER.format(version=cli.__version__)


def test_onboard_collects_plugin_config_and_writes_file(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.yml"

    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.chdir(tmp_path)
        framework = BubFramework(config_file=config_file)
        framework.load_hooks()

        class OnboardPlugin:
            @hookimpl
            def onboard_config(self, current_config):
                assert current_config == {}
                return {
                    "model": cli.typer.prompt("Model", default="openai:gpt-5"),
                    "telegram": {"token": cli.typer.prompt("Telegram token", hide_input=True)},
                }

        framework._plugin_manager.register(OnboardPlugin(), name="onboard-plugin")
        app = framework.create_cli_app()

        answers = iter([
            "openai:gpt-5",
            "123:abc",
            "openai:gpt-5",
            "",
            "",
        ])
        monkeypatch.setattr(
            cli.typer,
            "prompt",
            lambda message, default=None, hide_input=False, show_default=True: next(answers),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "text",
            lambda message, default="": _FakeQuestion(default),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "autocomplete",
            lambda message, choices, default="", match_middle=False: _FakeQuestion(default),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "select",
            lambda message, choices, default="": _FakeQuestion(default),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "checkbox",
            lambda message, choices, validate=None: _FakeQuestion(["telegram"]),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "confirm",
            lambda message, default=False: _FakeQuestion(default),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "password",
            lambda message, default="": _FakeQuestion(default),
        )

        result = CliRunner().invoke(app, ["onboard"])

        loaded = configure.load(config_file)

    assert result.exit_code == 0
    assert _rendered_onboard_banner() in result.stdout
    assert f"Saved config to {config_file.resolve()}" in result.stdout
    assert loaded == {
        "model": "openai:gpt-5",
        "api_format": "completion",
        "enabled_channels": "telegram",
        "stream_output": False,
        "telegram": {"token": "123:abc"},
    }


def test_onboard_collects_builtin_runtime_config(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.yml"

    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.chdir(tmp_path)
        framework = BubFramework(config_file=config_file)
        framework.load_hooks()
        app = framework.create_cli_app()

        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "text",
            lambda message, default="": _FakeQuestion(
                {
                    "LLM model": "openrouter/free",
                    "API base (optional)": "https://openrouter.ai/api/v1",
                }.get(message, default)
            ),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "autocomplete",
            lambda message, choices, default="", match_middle=False: _FakeQuestion("openrouter"),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "select",
            lambda message, choices, default="": _FakeQuestion("responses"),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "checkbox",
            lambda message, choices, validate=None: _FakeQuestion(["telegram", "cli"]),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "confirm",
            lambda message, default=False: _FakeQuestion(True),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "password",
            lambda message, default="": _FakeQuestion("sk-test"),
        )

        result = CliRunner().invoke(app, ["onboard"])

        loaded = configure.load(config_file)

    assert result.exit_code == 0
    assert loaded == {
        "model": "openrouter:openrouter/free",
        "api_format": "responses",
        "enabled_channels": "telegram,cli",
        "stream_output": True,
        "api_key": "sk-test",
        "api_base": "https://openrouter.ai/api/v1",
    }


def test_onboard_aborts_immediately_when_builtin_prompt_is_interrupted(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.yml"
    asked_messages: list[str] = []

    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.chdir(tmp_path)
        framework = BubFramework(config_file=config_file)
        framework.load_hooks()
        app = framework.create_cli_app()

        def fake_autocomplete(
            message: str, choices: list[str], default: str = "", match_middle: bool = False
        ) -> _FakeQuestion:
            asked_messages.append(message)
            return _FakeQuestion(default)

        def fake_select(message: str, choices: list[str], default: str = "") -> _FakeQuestion:
            asked_messages.append(message)
            return _FakeQuestion(default)

        def fake_checkbox(message: str, choices: list[object], validate=None) -> _FakeQuestion:
            asked_messages.append(message)
            return _FakeQuestion(["telegram"])

        def fake_confirm(message: str, default: bool = False) -> _FakeQuestion:
            asked_messages.append(message)
            return _FakeQuestion(default)

        def fake_text(message: str, default: str = "") -> _FakeQuestion:
            asked_messages.append(message)
            if message == "API base (optional)":
                raise AssertionError("Onboarding should stop after interruption")
            return _FakeQuestion("openrouter:openrouter/free")

        def fake_password(message: str, default: str = "") -> _FakeQuestion:
            asked_messages.append(message)
            return _FakeQuestion(None)

        monkeypatch.setattr(builtin_hook_impl.questionary, "autocomplete", fake_autocomplete)
        monkeypatch.setattr(builtin_hook_impl.questionary, "select", fake_select)
        monkeypatch.setattr(builtin_hook_impl.questionary, "checkbox", fake_checkbox)
        monkeypatch.setattr(builtin_hook_impl.questionary, "confirm", fake_confirm)
        monkeypatch.setattr(builtin_hook_impl.questionary, "text", fake_text)
        monkeypatch.setattr(builtin_hook_impl.questionary, "password", fake_password)

        result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 1
    assert _rendered_onboard_banner() in result.stdout
    assert asked_messages == [
        "LLM provider",
        "LLM model",
        "API key (optional)",
    ]
    assert not config_file.exists()


def test_onboard_collects_builtin_runtime_config_with_custom_provider(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.yml"

    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.chdir(tmp_path)
        framework = BubFramework(config_file=config_file)
        framework.load_hooks()
        app = framework.create_cli_app()

        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "autocomplete",
            lambda message, choices, default="", match_middle=False: _FakeQuestion("custom"),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "select",
            lambda message, choices, default="": _FakeQuestion("messages"),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "checkbox",
            lambda message, choices, validate=None: _FakeQuestion(["telegram"]),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "confirm",
            lambda message, default=False: _FakeQuestion(False),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "text",
            lambda message, default="": _FakeQuestion(
                {
                    "Custom provider": "acme",
                    "LLM model": "ultra-1",
                }.get(message, default)
            ),
        )
        monkeypatch.setattr(
            builtin_hook_impl.questionary,
            "password",
            lambda message, default="": _FakeQuestion(""),
        )

        result = CliRunner().invoke(app, ["onboard"])

        loaded = configure.load(config_file)

    assert result.exit_code == 0
    assert _rendered_onboard_banner() in result.stdout
    assert loaded == {
        "model": "acme:ultra-1",
        "api_format": "messages",
        "enabled_channels": "telegram",
        "stream_output": False,
    }


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
