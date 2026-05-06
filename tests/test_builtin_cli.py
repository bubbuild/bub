from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import typer
from inquirer_textual.common.InquirerResult import InquirerResult
from inquirer_textual.common.PromptSettings import PromptSettings
from typer.testing import CliRunner

import bub.builtin.auth as auth
import bub.builtin.cli as cli
import bub.configure as configure
import bub.inquirer as bub_inquirer
from bub.framework import BubFramework
from bub.hookspecs import hookimpl


def _fake_result(answer: Any, command: str | None = "enter") -> InquirerResult[Any]:
    return InquirerResult(None, answer, command)


def _assert_checkbox_hint(settings: PromptSettings | None) -> None:
    assert settings is not None
    assert settings.shortcuts is not None
    assert [(shortcut.key, shortcut.command, shortcut.description) for shortcut in settings.shortcuts] == [
        ("space", "toggle", "Space check/uncheck")
    ]


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
            bub_inquirer,
            "ask_text",
            lambda message, default="": default,
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_fuzzy",
            lambda message, choices, default=None: default,
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_select",
            lambda message, choices, default="": default,
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_checkbox",
            lambda message, choices, enabled=None, validate=None: ["telegram"],
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_confirm",
            lambda message, default=False: default,
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_secret",
            lambda message: "",
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
            bub_inquirer,
            "ask_text",
            lambda message, default="": {
                "LLM model": "openrouter/free",
                "API base (optional)": "https://openrouter.ai/api/v1",
            }.get(message, default),
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_fuzzy",
            lambda message, choices, default=None: "openrouter",
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_select",
            lambda message, choices, default="": "responses",
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_checkbox",
            lambda message, choices, enabled=None, validate=None: ["telegram", "cli"],
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_confirm",
            lambda message, default=False: True,
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_secret",
            lambda message: "sk-test",
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

        def fake_fuzzy(message: str, choices: list[str], default: str | None = None) -> str:
            asked_messages.append(message)
            assert default is not None
            return default

        def fake_select(message: str, choices: list[str], default: str = "") -> str:
            asked_messages.append(message)
            return default

        def fake_checkbox(
            message: str,
            choices: list[object],
            enabled=None,
            validate=None,
        ) -> list[str]:
            asked_messages.append(message)
            return ["telegram"]

        def fake_confirm(message: str, default: bool = False) -> bool:
            asked_messages.append(message)
            return default

        def fake_text(message: str, default: str = "") -> str:
            asked_messages.append(message)
            if message == "API base (optional)":
                raise AssertionError("Onboarding should stop after interruption")
            return "openrouter:openrouter/free"

        def fake_secret(message: str) -> str:
            asked_messages.append("API key (optional)")
            raise typer.Abort()

        monkeypatch.setattr(bub_inquirer, "ask_fuzzy", fake_fuzzy)
        monkeypatch.setattr(bub_inquirer, "ask_select", fake_select)
        monkeypatch.setattr(bub_inquirer, "ask_checkbox", fake_checkbox)
        monkeypatch.setattr(bub_inquirer, "ask_confirm", fake_confirm)
        monkeypatch.setattr(bub_inquirer, "ask_text", fake_text)
        monkeypatch.setattr(bub_inquirer, "ask_secret", fake_secret)

        result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 1
    assert _rendered_onboard_banner() in result.stdout
    assert asked_messages == [
        "LLM provider",
        "LLM model",
        "API key (optional)",
    ]
    assert not config_file.exists()


def test_run_command_processes_inbound_inside_framework_runtime(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    framework = BubFramework(config_file=config_file)
    observed: dict[str, Any] = {}

    class RecordingTapeStore:
        def __init__(self) -> None:
            self.enter_count = 0
            self.exit_count = 0

    tape_store = RecordingTapeStore()

    class RunPlugin:
        @hookimpl
        def register_cli_commands(self, app: typer.Typer) -> None:
            app.command("run")(cli.run)

        @hookimpl
        def provide_tape_store(self):
            tape_store.enter_count += 1
            try:
                yield tape_store
            finally:
                tape_store.exit_count += 1

        @hookimpl
        def build_prompt(self, message, session_id, state) -> str:
            observed["session_id"] = session_id
            observed["message_content"] = message.content
            observed["sender_id"] = message.context["sender_id"]
            return "prompt"

        @hookimpl
        async def run_model(self, prompt, session_id, state) -> str:
            observed["tape_store"] = framework.get_tape_store()
            return "model output"

        @hookimpl
        def render_outbound(self, message, session_id, state, model_output):
            return [{"channel": "stdout", "chat_id": "local", "content": model_output}]

        @hookimpl
        async def dispatch_outbound(self, message) -> bool:
            return True

    framework._plugin_manager.register(RunPlugin(), name="run-plugin")
    app = framework.create_cli_app()

    result = CliRunner().invoke(
        app,
        ["run", "hello", "--channel", "cli", "--chat-id", "room", "--sender-id", "frost"],
    )

    assert result.exit_code == 0
    assert "[stdout:local]\nmodel output" in result.stdout
    assert observed == {
        "session_id": "cli:room",
        "message_content": "hello",
        "sender_id": "frost",
        "tape_store": tape_store,
    }
    assert tape_store.enter_count == 1
    assert tape_store.exit_count == 1


def test_onboard_collects_builtin_runtime_config_with_custom_provider(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.yml"

    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.chdir(tmp_path)
        framework = BubFramework(config_file=config_file)
        framework.load_hooks()
        app = framework.create_cli_app()

        monkeypatch.setattr(
            bub_inquirer,
            "ask_fuzzy",
            lambda message, choices, default=None: "custom",
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_select",
            lambda message, choices, default="": "messages",
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_checkbox",
            lambda message, choices, enabled=None, validate=None: ["telegram"],
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_confirm",
            lambda message, default=False: False,
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_text",
            lambda message, default="": {
                "Custom provider": "acme",
                "LLM model": "ultra-1",
            }.get(message, default),
        )
        monkeypatch.setattr(
            bub_inquirer,
            "ask_secret",
            lambda message: "",
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
