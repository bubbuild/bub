from __future__ import annotations

import asyncio
import re
import sys
from contextlib import redirect_stdout
from io import StringIO

import pytest
import typer
from prompt_toolkit.application import create_app_session
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.patch_stdout import patch_stdout
from typer.main import get_command

from bub import inquirer
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.errors import BubError, ErrorKind
from bub.framework import BubFramework
from bub.hooks import hookimpl
from bub.program_status import listener_ready, message_failed, message_status, program_status, waiting
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState


class Terminal(StringIO):
    def isatty(self):
        return True


def reports(output):
    return [
        dict(pair.split("=", 1) for pair in body.split(":"))
        for body in re.findall(r"\x1b]7501;(.*?)(?:\x1b\\|\x07)", output.getvalue())
    ]


@pytest.fixture
def terminal():
    return Terminal()


@pytest.mark.parametrize(
    ("capability", "terminator"),
    [
        (b"\x1b]7501;%p1%s\x1b\\", "\x1b\\"),
        (b"\x1b]7501;%p1%s\x07", "\x07"),
        (None, "\x1b\\"),
        (OSError("terminfo unavailable"), "\x1b\\"),
    ],
)
def test_cli_reads_terminfo_once_and_reuses_template_for_messages(terminal, monkeypatch, capability, terminator):
    curses = pytest.importorskip("curses")
    calls = []
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setattr(terminal, "fileno", lambda: 1)
    monkeypatch.setattr(curses, "setupterm", lambda **kwargs: calls.append(kwargs))

    def lookup(name):
        calls.append(name)
        if isinstance(capability, Exception):
            raise capability
        return capability

    monkeypatch.setattr(curses, "tigetstr", lookup)
    app = BubFramework().create_cli_app()

    @app.command()
    def execute():
        with message_status(), waiting("question"):
            pass
        message_failed()
        return 42

    assert get_command(app).main(["execute"], standalone_mode=False) == 42
    assert calls == [{"fd": 1}, "Pst"]
    assert [item["state"] for item in reports(terminal)] == [
        "working",
        "working",
        "blocked",
        "working",
        "done",
        "error",
        "done",
    ]
    assert terminal.getvalue().count(terminator) == 7
    assert "7501;?" not in terminal.getvalue()


@pytest.mark.parametrize("output", [None, StringIO(), "broken"])
def test_unavailable_output_does_not_prevent_command_execution(monkeypatch, output):
    if output == "broken":
        output = Terminal()

        def disconnected():
            raise OSError("terminal closed")

        monkeypatch.setattr(output, "isatty", disconnected)
    monkeypatch.setattr(sys, "stdout", output)
    app = BubFramework().create_cli_app()
    app.command("execute")(lambda: 42)
    assert get_command(app).main(["execute"], standalone_mode=False) == 42
    if isinstance(output, StringIO):
        assert output.getvalue() == ""


@pytest.mark.parametrize(
    ("error", "state"),
    [(None, "done"), (RuntimeError("failed"), "error"), (typer.Exit(2), "error"), (typer.Abort(), "idle")],
)
def test_command_reports_after_context_cleanup(terminal, monkeypatch, error, state):
    monkeypatch.setattr(sys, "stdout", terminal)
    app = BubFramework().create_cli_app()

    @app.command()
    def execute(ctx: typer.Context):
        ctx.find_root().call_on_close(lambda: terminal.write("cleanup"))
        if error is not None:
            raise error
        return 42

    command = get_command(app)
    if error is None or isinstance(error, typer.Exit):
        assert command.main(["execute"], standalone_mode=False) == (42 if error is None else 2)
    else:
        with pytest.raises(type(error)) as caught:
            command.main(["execute"], standalone_mode=False)
        assert caught.value is error
    assert [item["state"] for item in reports(terminal)] == ["working", state]
    assert terminal.getvalue().index("cleanup") < terminal.getvalue().index(f"state={state}")
    with waiting("question"):
        pass
    assert len(reports(terminal)) == 2


def test_status_uses_captured_output_during_prompt_redirection(terminal):
    with (
        redirect_stdout(terminal),
        program_status(sys.stdout),
        create_app_session(output=Vt100_Output.from_pty(terminal)),
        patch_stdout(raw=True),
        waiting("auth"),
    ):
        assert reports(terminal)[-1] == {"state": "blocked", "app": "bub", "kind": "auth"}
    assert terminal.getvalue() == (
        "\x1b]7501;state=working:app=bub\x1b\\"
        "\x1b]7501;state=blocked:app=bub:kind=auth\x1b\\"
        "\x1b]7501;state=working:app=bub\x1b\\"
        "\x1b]7501;state=done:app=bub\x1b\\"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("recovered", [False, True])
async def test_final_model_result_is_reported_after_original_processing(terminal, streaming, recovered):
    order = []
    notices = []

    class Model:
        @hookimpl
        def run_model_stream(self, prompt, session_id, state):
            result = StreamState(error=BubError(ErrorKind.PROVIDER, "failed"))

            async def events():
                yield StreamEvent("error", {"kind": "provider", "message": "failed"})
                yield StreamEvent("text", {"delta": "response"})
                if recovered:
                    result.error = None

            async def close():
                order.append("close")

            return AsyncStreamEvents(events(), state=result, on_close=close)

        @hookimpl
        def save_state(self, session_id, state, message, model_output):
            order.append("save")

        @hookimpl
        def dispatch_outbound(self, message):
            assert reports(terminal)[-1]["state"] == "working"
            order.append("dispatch")

        @hookimpl
        def on_error(self, stage, error, message):
            notices.append(stage)
            raise OSError("error display failed")

    framework = BubFramework()
    framework.plugin_manager.register(Model())
    with program_status(terminal):
        result = await framework.process_inbound({"content": "request"}, stream_output=streaming)
    assert result.model_output == "response" and not hasattr(result, "error")
    assert order == ["close", "save", "dispatch"]
    assert notices == (["run_model"] if streaming else [])
    assert [item["state"] for item in reports(terminal)] == ["working", "done" if recovered else "error"]


@pytest.mark.asyncio
async def test_concurrent_messages_preserve_each_result_and_listener_idle(terminal):
    ready = [asyncio.Event(), asyncio.Event()]
    resume = [asyncio.Event(), asyncio.Event()]

    class Model:
        @hookimpl
        async def run_model(self, prompt, session_id, state):
            index = int(prompt)
            ready[index].set()
            await resume[index].wait()
            if index == 0:
                raise OSError("failed")
            return "response"

    framework = BubFramework()
    framework.plugin_manager.register(Model())
    manager = ChannelManager(framework)
    with program_status(terminal):
        listener_ready()
        tasks = [asyncio.create_task(manager._run_message(ChannelMessage(str(i), "cli", str(i)))) for i in range(2)]
        await asyncio.gather(*(event.wait() for event in ready))
        ids = [item["id"] for item in reports(terminal) if "id" in item]
        assert len(set(ids)) == 2
        resume[0].set()
        with pytest.raises(OSError):
            await tasks[0]
        updates = {item.get("id"): item["state"] for item in reports(terminal)}
        assert updates[ids[1]] == "working"
        resume[1].set()
        await tasks[1]
    updates = {item.get("id"): item["state"] for item in reports(terminal)}
    assert updates[None] == "idle"
    assert {updates[key] for key in ids} == {"done", "error"}


def test_permission_confirmation_uses_existing_prompt_once(terminal, monkeypatch):
    from inquirer_textual.common.InquirerResult import InquirerResult

    calls = []

    def confirm(*args, **kwargs):
        calls.append((args, kwargs))
        assert reports(terminal)[-1]["kind"] == "permission"
        return InquirerResult(None, True, "enter")

    monkeypatch.setattr(inquirer.prompts, "confirm", confirm)
    with program_status(terminal), waiting("permission"):
        assert inquirer.ask_confirm("Install?", default=False) is True
    assert calls == [(("Install?",), {"default": False})]
    assert [item["state"] for item in reports(terminal)] == ["working", "blocked", "working", "done"]


@pytest.mark.asyncio
async def test_sdk_has_no_terminal_scope_after_cli_creation(terminal, monkeypatch):
    monkeypatch.setattr(sys, "stdout", terminal)
    framework = BubFramework()
    framework.create_cli_app()
    result = await framework.process_inbound({"content": "request"})
    assert result.model_output == "request" and not hasattr(result, "error")
    assert reports(terminal) == []
