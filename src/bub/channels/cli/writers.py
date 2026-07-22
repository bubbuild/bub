"""Streaming text rendering strategies for CLI output.

This module provides pluggable writers that control how streaming text
from model responses is formatted and committed to the terminal.

- ``PlainTextWriter``: line-based plain text (original bub behavior)
- ``MarkdownWriter``: block-based Markdown with incremental commitment
"""

from __future__ import annotations

import io
import re
from typing import Any, Protocol, runtime_checkable

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

_MARKDOWN_CODE_THEME = "ansi_dark"
_FENCE_START_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _markdown(content: str) -> Markdown:
    return Markdown(content, code_theme=_MARKDOWN_CODE_THEME)


def _committable_prefix_length(content: str) -> int:
    """Return the last completed blank-line boundary outside fenced code."""
    boundary = 0
    offset = 0
    fence_char: str | None = None
    fence_length = 0

    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if fence_char is None:
            match = _FENCE_START_RE.match(body)
            if match is not None and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                marker = match.group(1)
                fence_char = marker[0]
                fence_length = len(marker)
            elif not body.strip() and line.endswith(("\n", "\r")):
                boundary = offset + len(line)
        else:
            stripped = body.lstrip(" ")
            indent = len(body) - len(stripped)
            marker_length = len(stripped) - len(stripped.lstrip(fence_char))
            if indent <= 3 and marker_length >= fence_length and not stripped[marker_length:].strip():
                fence_char = None
                fence_length = 0
        offset += len(line)

    return boundary


@runtime_checkable
class StreamWriter(Protocol):
    """Protocol for streaming text rendering strategies.

    A StreamWriter accumulates text deltas and decides when content is
    ready to be permanently committed vs. kept in a re-renderable live area.
    """

    def append(self, text: str) -> None:
        """Accumulate a text delta."""
        ...

    def can_commit(self) -> bool:
        """Return True when committable content is ready."""
        ...

    def render_committed(self) -> Any:
        """Return a renderable for the next committable content."""
        ...

    def render_partial(self) -> Any:
        """Return a renderable for current live (uncommitted) content."""
        ...

    def commit(self) -> bool:
        """Advance past committed content. Returns True if content was committed."""
        ...

    def flush(self) -> Any | None:
        """Force-commit all remaining content. Returns renderable or None."""
        ...

    def has_content(self) -> bool:
        """Return True if there is any uncommitted content."""
        ...

    def reset(self) -> None:
        """Reset to initial empty state."""
        ...

    def row_count(self, renderable: Any, console_width: int) -> int:
        """Calculate terminal rows needed for a renderable."""
        ...


class PlainTextWriter:
    """Line-based plain text writer.

    Commits on newline boundaries. Each completed line becomes permanently
    printed text. The incomplete tail is re-rendered on each delta.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._committed_up_to = 0

    def append(self, text: str) -> None:
        self._buffer += text

    def can_commit(self) -> bool:
        return "\n" in self._buffer[self._committed_up_to:]

    def render_committed(self) -> str:
        uncommitted = self._buffer[self._committed_up_to:]
        last_nl = uncommitted.rfind("\n")
        return uncommitted[: last_nl + 1]

    def render_partial(self) -> str:
        uncommitted = self._buffer[self._committed_up_to:]
        last_nl = uncommitted.rfind("\n")
        return uncommitted[last_nl + 1:]

    def commit(self) -> bool:
        uncommitted = self._buffer[self._committed_up_to:]
        last_nl = uncommitted.rfind("\n")
        if last_nl < 0:
            return False
        self._committed_up_to += last_nl + 1
        return True

    def flush(self) -> str | None:
        remaining = self._buffer[self._committed_up_to:]
        if not remaining:
            return None
        self._committed_up_to = len(self._buffer)
        return remaining + "\n"

    def has_content(self) -> bool:
        return bool(self._buffer[self._committed_up_to:])

    def reset(self) -> None:
        self._buffer = ""
        self._committed_up_to = 0

    def row_count(self, renderable: Any, console_width: int) -> int:
        text = str(renderable).rstrip("\n")
        if not text:
            return 0
        from prompt_toolkit.utils import get_cwidth

        columns = max(1, console_width)
        return max(1, (get_cwidth(text) + columns - 1) // columns)


class MarkdownWriter:
    """Block-based Markdown writer with incremental commitment.

    Commits completed top-level blocks (separated by blank lines) via
    ``rich.Markdown``. The incomplete tail is re-rendered on each delta.

    Fence-aware: keeps an open fenced code block in the live area so Rich can
    render and highlight it before the closing fence arrives.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._committed_text = ""
        self._to_commit = ""
        self._partial = ""

    def append(self, text: str) -> None:
        self._buffer += text
        self._reparse()

    def _reparse(self) -> None:
        uncommitted = self._buffer[len(self._committed_text):]
        split_at = _committable_prefix_length(uncommitted)
        self._to_commit = uncommitted[:split_at]
        self._partial = uncommitted[split_at:]

    def can_commit(self) -> bool:
        return bool(self._to_commit) and bool(self._to_commit.strip())

    def render_committed(self) -> Markdown:
        return _markdown(self._to_commit.rstrip())

    def render_partial(self) -> Markdown | Text:
        if not self._partial.strip():
            return Text("")
        return _markdown(self._partial)

    def commit(self) -> bool:
        if not self.can_commit():
            return False
        self._committed_text += self._to_commit
        self._to_commit = ""
        return True

    def flush(self) -> Markdown | None:
        uncommitted = self._buffer[len(self._committed_text):]
        if not uncommitted.strip():
            return None
        self._committed_text = self._buffer
        self._to_commit = ""
        self._partial = ""
        return _markdown(uncommitted.rstrip())

    def has_content(self) -> bool:
        uncommitted = self._buffer[len(self._committed_text):]
        return bool(uncommitted.strip())

    def reset(self) -> None:
        self._buffer = ""
        self._committed_text = ""
        self._to_commit = ""
        self._partial = ""

    def row_count(self, renderable: Any, console_width: int) -> int:
        if not renderable or (isinstance(renderable, Text) and not str(renderable).strip()):
            return 0
        tmp = Console(
            file=io.StringIO(),
            width=console_width,
            force_terminal=True,
            color_system=None,
        )
        with tmp.capture() as captured:
            tmp.print(renderable, end="")
        lines = captured.get().split("\n")
        while lines and not lines[-1].strip():
            lines.pop()
        return max(1, len(lines)) if lines else 0
