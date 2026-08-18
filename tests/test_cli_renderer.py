from __future__ import annotations

from rich.console import Console

from bub.channels.cli.renderer import CliRenderer
from bub.channels.cli.writers import PanelEnd, PanelHead


def test_panel_head_and_end_commit_separate_terminal_lines() -> None:
    console = Console(record=True, force_terminal=False, width=40)
    console.print(PanelHead("assistant", border_style="blue"))
    console.print("streamed body")
    console.print(PanelEnd(border_style="blue"))
    console.print("next prompt")

    lines = console.export_text().splitlines()

    assert len(lines) == 4
    assert lines[0].startswith("╭")
    assert "assistant" in lines[0]
    assert lines[1] == "streamed body"
    assert lines[2].startswith("╰")
    assert lines[3] == "next prompt"


def test_tool_call_renderer_hides_success_output() -> None:
    console = Console(record=True, force_terminal=False, width=120)
    renderer = CliRenderer(console)

    renderer.tool_call_start(name="demo.echo", args=(), kwargs={"value": "hello"})
    renderer.tool_call_success(name="demo.echo", result={"secret": "hidden-result"}, elapsed_ms=12.3)

    output = console.export_text()

    assert '● demo.echo(value: "hello")' in output
    assert "  ⎿ completed in 12 ms" in output
    assert "hidden-result" not in output
    assert "Tool call:" not in output
    assert "Tool result:" not in output


def test_input_echo_prints_submitted_prompt_as_terminal_output() -> None:
    console = Console(record=True, force_terminal=False, width=120)
    renderer = CliRenderer(console)

    renderer.input_echo("bub > ", "hello")

    output = console.export_text()

    assert "bub > hello" in output
