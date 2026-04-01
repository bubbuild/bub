from __future__ import annotations

from pathlib import Path

import typer
from typer.testing import CliRunner

from bub.channels.base import Channel
from bub.framework import BubFramework
from bub.hookspecs import hookimpl


class NamedChannel(Channel):
    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label

    async def start(self, stop_event) -> None:
        return None

    async def stop(self) -> None:
        return None


def test_create_cli_app_sets_workspace_and_context(tmp_path: Path) -> None:
    framework = BubFramework()

    class CliPlugin:
        @hookimpl
        def register_cli_commands(self, app: typer.Typer) -> None:
            @app.command("workspace")
            def workspace_command(ctx: typer.Context) -> None:
                current = ctx.ensure_object(BubFramework)
                typer.echo(str(current.workspace))

    framework._plugin_manager.register(CliPlugin(), name="cli-plugin")
    app = framework.create_cli_app()

    result = CliRunner().invoke(app, ["--workspace", str(tmp_path), "workspace"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
    assert framework.workspace == tmp_path.resolve()


def test_get_channels_prefers_high_priority_plugin_for_duplicate_names() -> None:
    framework = BubFramework()

    class LowPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [NamedChannel("shared", "low"), NamedChannel("low-only", "low")]

    class HighPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [NamedChannel("shared", "high"), NamedChannel("high-only", "high")]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    channels = framework.get_channels(lambda message: None)

    assert set(channels) == {"shared", "low-only", "high-only"}
    assert channels["shared"].label == "high"
    assert channels["low-only"].label == "low"
    assert channels["high-only"].label == "high"


def test_get_system_prompt_uses_priority_order_and_skips_empty_results() -> None:
    framework = BubFramework()

    class LowPriorityPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str:
            return "low"

    class HighPriorityPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str | None:
            return "high"

    class EmptyPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str | None:
            return None

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")
    framework._plugin_manager.register(EmptyPlugin(), name="empty")

    prompt = framework.get_system_prompt(prompt="hello", state={})

    assert prompt == "low\n\nhigh"


def test_builtin_cli_exposes_login_and_gateway_command() -> None:
    framework = BubFramework()
    framework.load_hooks()
    app = framework.create_cli_app()
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    gateway_result = runner.invoke(app, ["gateway", "--help"])

    assert help_result.exit_code == 0
    assert "login" in help_result.stdout
    assert "gateway" in help_result.stdout
    assert "│ message" not in help_result.stdout
    assert gateway_result.exit_code == 0
    assert "bub gateway" in gateway_result.stdout
    assert "Start message listeners" in gateway_result.stdout
