"""CLI rendering helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from bub.channels.message import MessageKind

MAX_TOOL_PAYLOAD_CHARS = 4000
MAX_TOOL_CALL_CHARS = 1200


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

    def input_echo(self, prompt: str, text: str, steering: bool = False) -> None:
        if not text.strip():
            return
        mid = "[grey](steering)[/] " if steering else ""
        self.console.print(f"[dim][bold]{prompt}[/]{mid}{text}[/]", new_line_start=True)

    def tool_call_start(self, *, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.console.print(Text(_format_tool_call(name, args, kwargs), style="magenta"), new_line_start=True)

    def tool_call_success(self, *, name: str, result: Any, elapsed_ms: float) -> None:
        rendered = _format_tool_payload(result)
        self._tool_result(f"completed in {elapsed_ms:.0f} ms", rendered, style="green")

    def tool_call_error(self, *, name: str, error: BaseException, elapsed_ms: float) -> None:
        rendered = _format_tool_payload({"type": error.__class__.__name__, "message": str(error)})
        self._tool_result(f"failed in {elapsed_ms:.0f} ms", rendered, style="red")

    def print_head(self, kind: MessageKind) -> None:
        if kind == "command":
            self.console.print("[cyan bold]Command >[/]", new_line_start=True)
        elif kind == "error":
            self.console.print("[red bold]Error >[/]", new_line_start=True)
        else:
            self.console.print("[blue bold]Assistant >[/]", new_line_start=True)

    def log(self, message: object) -> None:
        text = str(message).rstrip()
        if text:
            self.console.print(text, new_line_start=True)

    def _tool_result(self, label: str, rendered: str, *, style: str) -> None:
        lines = rendered.splitlines() or [""]
        self.console.print(Text(f"  ⎿ {label}", style=style), highlight=False)
        for line in lines:
            self.console.print(Text(f"    {line}", style="bright_black"), highlight=False)


def _format_tool_call(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    params = _format_tool_params(args, kwargs)
    if not params:
        return f"● {name}()"

    inline = f"● {name}({', '.join(params)})"
    if len(inline) <= 120 and "\n" not in inline:
        return inline

    body = "\n".join(f"  {param}," for param in params)
    return _truncate(f"● {name}(\n{body}\n)", max_chars=MAX_TOOL_CALL_CHARS)


def _format_tool_params(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[str]:
    params: list[str] = []
    for index, value in enumerate(args, start=1):
        params.append(f"arg{index}: {_format_tool_value(value)}")
    for key, value in kwargs.items():
        params.append(f"{key}: {_format_tool_value(value)}")
    return params


def _format_tool_payload(payload: Any, *, max_chars: int = MAX_TOOL_PAYLOAD_CHARS) -> str:
    return _format_tool_value(payload, max_chars=max_chars, indent=2)


def _format_tool_value(payload: Any, *, max_chars: int = 800, indent: int | None = None) -> str:
    try:
        rendered = json.dumps(payload, ensure_ascii=False, indent=indent, default=repr)
    except TypeError:
        rendered = repr(payload)
    return _truncate(rendered, max_chars=max_chars)


def _truncate(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    suffix = f"\n... truncated {omitted} chars"
    return text[: max_chars - len(suffix)].rstrip() + suffix
