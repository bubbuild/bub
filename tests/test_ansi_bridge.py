from __future__ import annotations

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from rich.console import Group
from rich.markdown import Markdown
from rich.style import Style
from rich.text import Text

from bub.channels.cli.ansi_bridge import render_to_ansi


def _visible_text(value: str) -> str:
    return "".join(
        fragment[1]
        for fragment in to_formatted_text(ANSI(value))
        if "[ZeroWidthEscape]" not in fragment[0]
    )


def test_render_to_ansi_renders_markdown_and_groups(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = render_to_ansi(
        Group(Markdown("**first**"), Text("second", style="green")),
        width=40,
    )

    visible = _visible_text(rendered)
    assert "first" in visible
    assert "second" in visible
    assert visible.index("first") < visible.index("second")
    assert "\x1b[" in rendered


def test_render_to_ansi_marks_osc8_links_as_zero_width(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")

    rendered = render_to_ansi(
        Text("OpenAI", style=Style(link="https://openai.com")),
        width=40,
    )
    fragments = to_formatted_text(ANSI(rendered))

    zero_width = [fragment[1] for fragment in fragments if "[ZeroWidthEscape]" in fragment[0]]
    assert len(zero_width) == 2
    assert zero_width[0].startswith("\x1b]8;")
    assert "https://openai.com" in zero_width[0]
    assert zero_width[1].startswith("\x1b]8;;")
    assert _visible_text(rendered) == "OpenAI"


def test_render_to_ansi_honors_width_for_ascii_and_cjk(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")

    ascii_lines = _visible_text(render_to_ansi(Text("1234567890"), width=4)).splitlines()
    cjk_lines = _visible_text(render_to_ansi(Text("春风一夜入江城"), width=6)).splitlines()

    assert ascii_lines == ["1234", "5678", "90"]
    assert cjk_lines == ["春风一", "夜入江", "城"]


def test_render_to_ansi_accepts_default_width() -> None:
    assert "hello" in _visible_text(render_to_ansi(Text("hello")))
