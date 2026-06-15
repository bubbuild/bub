"""CLI rendering helpers."""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
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

    def command_output(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(f"[cyan bold]Command >[/]\n{text}")

    def assistant_output(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(f"[blue bold]Assistant >[/]\n{text}")

    def error(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(f"[red bold]Error >[/]\n{text}")

    def print_head(self, kind: MessageKind) -> None:
        if kind == "command":
            self.console.print("[cyan bold]Command >[/]")
        elif kind == "error":
            self.console.print("[red bold]Error >[/]")
        else:
            self.console.print("[blue bold]Assistant >[/]")

    def log(self, message: object) -> None:
        text = str(message).rstrip()
        if text:
            self.console.print(text, new_line_start=True)
