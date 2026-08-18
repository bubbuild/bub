"""Response-scoped Markdown buffering for CLI streaming output."""

from __future__ import annotations

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown
from rich.panel import Panel
from rich.segment import Segment
from rich.text import Text

_MARKDOWN_CODE_THEME = "ansi_dark"


def _markdown(content: str) -> Markdown:
    return Markdown(content, code_theme=_MARKDOWN_CODE_THEME)


class PanelHead:
    """The top border of a Panel, printed before its content is streamed."""

    def __init__(self, title: str, *, border_style: str, width: int | None = None) -> None:
        self._title = title
        self._border_style = border_style
        self._width = width

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = self._width if self._width is not None else options.max_width
        panel = Panel("", title=self._title, border_style=self._border_style, width=width)
        for line in console.render_lines(panel, options.update(width=width), pad=False)[:1]:
            yield from line
        yield Segment.line()


class PanelEnd:
    """The bottom border of a Panel, printed after its content is streamed."""

    def __init__(self, *, border_style: str, width: int | None = None) -> None:
        self._border_style = border_style
        self._width = width

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = self._width if self._width is not None else options.max_width
        panel = Panel("", border_style=self._border_style, width=width)
        for line in console.render_lines(panel, options.update(width=width), pad=False)[-1:]:
            yield from line
        yield Segment.line()


class MarkdownWriter:
    """Keep one response segment as a single Markdown document.

    Newlines are Markdown structure, not terminal commit boundaries. The
    buffer is drained only at an explicit model or tool boundary.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def append(self, text: str) -> None:
        self._buffer += text

    def render_live(self) -> Markdown | Text:
        if not self._buffer.strip():
            return Text("")
        return _markdown(self._buffer)

    def render_final(self) -> Markdown | None:
        if not self._buffer.strip():
            return None
        return _markdown(self._buffer.rstrip())

    def clear(self) -> None:
        self._buffer = ""

    def has_content(self) -> bool:
        return bool(self._buffer.strip())
