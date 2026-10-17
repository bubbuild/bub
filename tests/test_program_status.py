from __future__ import annotations

import asyncio
import re
import sys
from io import StringIO

import pytest
import typer
from typer.main import get_command

from bub.channels.base import Channel
from bub.channels.message import ChannelMessage
from bub.framework import BubFramework
from bub.hooks import hookimpl


class MessageChannel(Channel):
    name = "test"

    def __init__(self, deliver):
        self.deliver = deliver

    async def start(self, stop_event):
        self.delivery = asyncio.create_task(self.deliver(stop_event))

    async def stop(self):
        await self.delivery

    async def send(self, message):
        typer.echo(message.content)


def reports(output):
    return [
        dict(pair.split("=", 1) for pair in body.split(":"))
        for body in re.findall(r"\x1b]7501;(.*?)\x1b\\", output.getvalue())
    ]


@pytest.mark.parametrize("second_waits", ["model", "admission"])
def test_listener_stays_working_until_accepted_messages_finish(tmp_path, monkeypatch, second_waits):
    terminal = StringIO()
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdout", terminal)
    ready = asyncio.Event()
    failed = asyncio.Event()
    resume = asyncio.Event()
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()

    async def exchange(receive, stop_event):
        try:
            async with asyncio.timeout(2):
                while reports(terminal)[-1]["state"] != "idle":
                    await asyncio.sleep(0)
                await receive(ChannelMessage("first", "test", "fail"))
                await receive(ChannelMessage("second", "test", "succeed"))
                await failed.wait()
                await asyncio.sleep(0)
                assert reports(terminal)[-1]["state"] == "working"
                resume.set()
                while reports(terminal)[-1]["state"] != "done":
                    await asyncio.sleep(0)
                assert "response" in terminal.getvalue()
        finally:
            resume.set()
            stop_event.set()

    class Hooks:
        @hookimpl
        def provide_channels(self, message_handler):
            return [MessageChannel(lambda stop_event: exchange(message_handler, stop_event))]

        @hookimpl
        async def admit_message(self, session_id, message, turn):
            if session_id == "second" and second_waits == "admission":
                ready.set()
                await resume.wait()

        @hookimpl
        async def run_model(self, prompt, session_id, state):
            if session_id == "first":
                await ready.wait()
                failed.set()
                raise RuntimeError("failed")
            ready.set()
            await resume.wait()
            return "response"

    framework.plugin_manager.register(Hooks())
    app = framework.create_cli_app()

    get_command(app).main(["--workspace", str(tmp_path), "gateway", "--enable-channel", "test"], standalone_mode=False)

    assert reports(terminal)[-1]["state"] == "idle"
    assert all(item["state"] != "error" for item in reports(terminal))
