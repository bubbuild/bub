from __future__ import annotations

from rich.console import Console

from bub.channels.cli.renderer import CliRenderer


def test_tool_call_renderer_uses_compact_claude_style_output() -> None:
    console = Console(record=True, force_terminal=False, width=120)
    renderer = CliRenderer(console)

    renderer.tool_call_start(name="demo.echo", args=(), kwargs={"value": "hello"})
    renderer.tool_call_success(name="demo.echo", result={"ok": True}, elapsed_ms=12.3)

    output = console.export_text()

    assert '● demo.echo(value: "hello")' in output
    assert "  ⎿ completed in 12 ms" in output
    assert '"ok": true' in output
    assert "Tool call:" not in output
    assert "Tool result:" not in output


def test_input_echo_prints_submitted_prompt_as_terminal_output() -> None:
    console = Console(record=True, force_terminal=False, width=120)
    renderer = CliRenderer(console)

    renderer.input_echo("bub > ", "hello")

    output = console.export_text()

    assert "bub > hello" in output
