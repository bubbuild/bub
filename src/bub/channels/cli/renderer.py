"""CLI rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from bub.channels.message import MessageKind


@dataclass
class CliRenderer:
    """Rich-based renderer for interactive CLI."""

    console: Console

    def welcome(self, *, model: str, workspace: str) -> None:
        body = (
            f"workspace: {workspace}\n"
            f"model: {model}\n"
            "internal command prefix: ','\n"
            "shell command prefix: ',' at line start (Ctrl-X for shell mode)\n"
            "type ',help' for command list"
        )
        self.console.print(Panel(body, title="Bub", border_style="cyan"))

    def info(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(Text(text, style="bright_black"))

    def panel(self, kind: MessageKind, text: str) -> Panel:
        title, border_style = self._panel_style(kind)
        return Panel(text, title=title, border_style=border_style)

    def command_output(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(self.panel("command", text))

    def assistant_output(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(self.panel("normal", text))

    def error(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(self.panel("error", text))

    def start_stream(self, kind: MessageKind) -> Live:
        live = Live(
            self.panel(kind, ""),
            console=self.console,
            auto_refresh=False,
            transient=False,
            vertical_overflow="visible",
        )
        live.start()
        live.refresh()
        return live

    def update_stream(self, live: Live, *, kind: MessageKind, text: str) -> None:
        live.update(self.panel(kind, text), refresh=True)

    def finish_stream(self, live: Live, *, kind: MessageKind, text: str) -> None:
        live.update(self.panel(kind, text), refresh=True)
        live.stop()

    @staticmethod
    def _panel_style(kind: MessageKind) -> tuple[str, str]:
        match kind:
            case "error":
                return "Error", "red"
            case "command":
                return "Command", "green"
            case _:
                return "Assistant", "blue"
