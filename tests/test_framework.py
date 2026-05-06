from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
import typer
from republic import AsyncStreamEvents, StreamEvent, StreamState
from typer.testing import CliRunner

from bub import configure
from bub.builtin.settings import load_settings
from bub.channels.base import Channel
from bub.channels.message import ChannelMessage
from bub.channels.telegram import TelegramSettings
from bub.configure import ensure_config
from bub.framework import BubFramework
from bub.hookspecs import hookimpl


def make_named_channel(name: str, label: str) -> Channel:
    channel_name = name
    channel_label = label

    class NamedChannelImpl(Channel):
        name = channel_name

        def __init__(self) -> None:
            self.label = channel_label

        async def start(self, stop_event) -> None:
            return None

        async def stop(self) -> None:
            return None

    return NamedChannelImpl()


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

    async def message_handler(message) -> None:
        return None

    class LowPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [make_named_channel("shared", "low"), make_named_channel("low-only", "low")]

    class HighPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [make_named_channel("shared", "high"), make_named_channel("high-only", "high")]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    channels = framework.get_channels(message_handler)

    assert set(channels) == {"shared", "low-only", "high-only"}
    assert cast(Any, channels["shared"]).label == "high"
    assert cast(Any, channels["low-only"]).label == "low"
    assert cast(Any, channels["high-only"]).label == "high"


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


@pytest.mark.asyncio
async def test_running_enters_tape_store_once_and_reuses_it() -> None:
    framework = BubFramework()

    class RecordingTapeStore:
        def __init__(self) -> None:
            self.enter_count = 0
            self.exit_count = 0

    tape_store = RecordingTapeStore()

    class TapePlugin:
        @hookimpl
        def provide_tape_store(self):
            tape_store.enter_count += 1
            try:
                yield tape_store
            finally:
                tape_store.exit_count += 1

    framework._plugin_manager.register(TapePlugin(), name="tape")

    async with framework.running():
        assert framework.get_tape_store() is tape_store
        assert framework.get_tape_store() is tape_store
        assert tape_store.enter_count == 1
        assert tape_store.exit_count == 0

    assert tape_store.enter_count == 1
    assert tape_store.exit_count == 1


def test_builtin_cli_exposes_login_and_gateway_command(write_config) -> None:
    with patch.dict(os.environ, {}, clear=True):
        framework = BubFramework(config_file=write_config())
        framework.load_hooks()
        app = framework.create_cli_app()
        runner = CliRunner()

        help_result = runner.invoke(app, ["--help"])
        gateway_result = runner.invoke(app, ["gateway", "--help"])

    assert help_result.exit_code == 0
    assert "login" in help_result.stdout
    assert "gateway" in help_result.stdout
    assert "onboard" in help_result.stdout
    assert "│ message" not in help_result.stdout
    assert gateway_result.exit_code == 0
    assert "bub gateway" in gateway_result.stdout
    assert "Start message listeners" in gateway_result.stdout


def test_load_hooks_loads_root_and_named_config_sections(monkeypatch: pytest.MonkeyPatch, write_config) -> None:
    expected = "test-token"
    config_file = write_config(
        f"""
model: openai:gpt-5
telegram:
    token: {expected}
""".strip()
    )

    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.chdir(config_file.parent)
        framework = BubFramework(config_file=config_file)

        framework.load_hooks()

        assert load_settings().model == "openai:gpt-5"
        assert ensure_config(TelegramSettings).token == expected


def test_load_hooks_initializes_callable_plugins_after_config_load(
    monkeypatch: pytest.MonkeyPatch, write_config
) -> None:
    with patch.dict(os.environ, {}, clear=True):
        framework = BubFramework(config_file=write_config("model: openai:gpt-5"))

        class SettingsAwarePlugin:
            def __init__(self, _framework: BubFramework) -> None:
                self.model = load_settings().model

            @hookimpl
            def register_cli_commands(self, app: typer.Typer) -> None:
                return None

        entry_point = SimpleNamespace(name="config-plugin", load=lambda: SettingsAwarePlugin)
        monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: [entry_point])

        framework.load_hooks()

    assert framework._plugin_status["config-plugin"].is_success is True


def test_collect_onboard_config_passes_accumulated_updates_to_later_hooks(write_config) -> None:
    with patch.dict(os.environ, {}, clear=True):
        framework = BubFramework(config_file=write_config("model: openai:gpt-5"))
        observed_configs: list[tuple[str, dict[str, Any]]] = []

        class FirstPlugin:
            @hookimpl
            def onboard_config(self, current_config):
                observed_configs.append(("first", configure.merge({}, current_config)))
                return {"first": {"enabled": True}}

        class SecondPlugin:
            @hookimpl
            def onboard_config(self, current_config):
                observed_configs.append(("second", configure.merge({}, current_config)))
                return {"second": {"enabled": True}}

        framework._plugin_manager.register(FirstPlugin(), name="first")
        framework._plugin_manager.register(SecondPlugin(), name="second")

        result = framework.collect_onboard_config()

    assert observed_configs[0][1] == {}
    assert observed_configs[1][1] == {observed_configs[0][0]: {"enabled": True}}
    assert result == {
        "first": {"enabled": True},
        "second": {"enabled": True},
    }


@pytest.mark.asyncio
async def test_process_inbound_defaults_to_non_streaming_run_model() -> None:
    framework = BubFramework()
    saved_outputs: list[str] = []

    class NonStreamingPlugin:
        @hookimpl
        def resolve_session(self, message) -> str:
            return "session"

        @hookimpl
        def load_state(self, message, session_id) -> dict[str, str]:
            return {}

        @hookimpl
        def build_prompt(self, message, session_id, state) -> str:
            return "prompt"

        @hookimpl
        async def run_model(self, prompt, session_id, state) -> str:
            return "plain-text"

        @hookimpl
        async def save_state(self, session_id, state, message, model_output) -> None:
            saved_outputs.append(model_output)

        @hookimpl
        def render_outbound(self, message, session_id, state, model_output):
            return [{"content": model_output, "channel": "cli", "chat_id": "room"}]

        @hookimpl
        async def dispatch_outbound(self, message) -> bool:
            return True

    framework._plugin_manager.register(NonStreamingPlugin(), name="non-streaming")

    result = await framework.process_inbound(
        ChannelMessage(session_id="s", channel="cli", chat_id="room", content="hi")
    )

    assert result.model_output == "plain-text"
    assert saved_outputs == ["plain-text"]


@pytest.mark.asyncio
async def test_process_inbound_streams_when_requested() -> None:  # noqa: C901
    framework = BubFramework()
    stream_calls: list[str] = []
    wrapped_events: list[str] = []

    class StreamingPlugin:
        @hookimpl
        def resolve_session(self, message) -> str:
            return "session"

        @hookimpl
        def load_state(self, message, session_id) -> dict[str, str]:
            return {}

        @hookimpl
        def build_prompt(self, message, session_id, state) -> str:
            return "prompt"

        @hookimpl
        async def run_model_stream(self, prompt, session_id, state):
            stream_calls.append(prompt)

            async def iterator():
                yield StreamEvent("text", {"delta": "stream"})
                yield StreamEvent("text", {"delta": "ed"})
                yield StreamEvent("final", {"text": "streamed", "ok": True})

            return AsyncStreamEvents(iterator(), state=StreamState())

        @hookimpl
        async def save_state(self, session_id, state, message, model_output) -> None:
            return None

        @hookimpl
        def render_outbound(self, message, session_id, state, model_output):
            return [{"content": model_output, "channel": "cli", "chat_id": "room"}]

        @hookimpl
        async def dispatch_outbound(self, message) -> bool:
            return True

    class RecordingRouter:
        def wrap_stream(self, message, stream):
            async def iterator():
                async for event in stream:
                    wrapped_events.append(event.kind)
                    yield event

            return iterator()

        async def dispatch_output(self, message) -> bool:
            return True

        async def quit(self, session_id: str) -> None:
            return None

    framework._plugin_manager.register(StreamingPlugin(), name="streaming")
    framework.bind_outbound_router(RecordingRouter())

    result = await framework.process_inbound(
        ChannelMessage(session_id="s", channel="cli", chat_id="room", content="hi"),
        stream_output=True,
    )

    assert stream_calls == ["prompt"]
    assert wrapped_events == ["text", "text", "final"]
    assert result.model_output == "streamed"
