"""Unit tests for streaming text writers."""

from __future__ import annotations

import pytest

from bub.channels.cli import _StreamPrinter
from bub.channels.cli.writers import (
    MarkdownWriter,
    PlainTextWriter,
    StreamWriter,
)
from bub.streaming import StreamEvent


class TestPlainTextWriter:
    def test_cannot_commit_without_newline(self):
        w = PlainTextWriter()
        w.append("hello")
        assert not w.can_commit()

    def test_can_commit_with_newline(self):
        w = PlainTextWriter()
        w.append("hello\n")
        assert w.can_commit()

    def test_splits_on_newline(self):
        w = PlainTextWriter()
        w.append("hello\nworld")
        assert w.render_committed() == "hello\n"
        assert w.render_partial() == "world"

    def test_commit_advances_state(self):
        w = PlainTextWriter()
        w.append("line1\nline2\nline3")
        assert w.render_committed() == "line1\nline2\n"
        w.commit()
        assert w.render_partial() == "line3"
        assert not w.can_commit()

    def test_flush_adds_trailing_newline(self):
        w = PlainTextWriter()
        w.append("partial")
        assert w.flush() == "partial\n"
        assert not w.has_content()

    def test_flush_returns_none_when_empty(self):
        w = PlainTextWriter()
        assert w.flush() is None

    def test_streaming_simulation(self):
        w = PlainTextWriter()
        w.append("hel")
        assert not w.can_commit()
        w.append("lo\nwor")
        assert w.can_commit()
        assert w.render_committed() == "hello\n"
        assert w.render_partial() == "wor"
        w.commit()
        w.append("ld\n")
        assert w.can_commit()
        assert w.render_committed() == "world\n"

    def test_row_count_basic(self):
        w = PlainTextWriter()
        assert w.row_count("hello", 80) == 1
        assert w.row_count("", 80) == 0

    def test_row_count_wrapping(self):
        w = PlainTextWriter()
        assert w.row_count("a" * 100, 80) == 2

    def test_row_count_cjk(self):
        w = PlainTextWriter()
        assert w.row_count("你好世界", 80) == 1  # 8 display cols
        assert w.row_count("你" * 40, 80) == 1  # 80 display cols
        assert w.row_count("你" * 41, 80) == 2  # 82 → wraps

    def test_reset(self):
        w = PlainTextWriter()
        w.append("hello\n")
        w.reset()
        assert not w.has_content()
        assert not w.can_commit()

    def test_protocol_compliance(self):
        assert isinstance(PlainTextWriter(), StreamWriter)


class TestMarkdownWriter:
    @staticmethod
    def _render_text(writer: MarkdownWriter) -> str:
        from io import StringIO

        from rich.console import Console

        output = StringIO()
        Console(file=output, width=80, force_terminal=True, color_system=None).print(writer.render_partial())
        return output.getvalue()

    def test_single_paragraph_stays_partial_without_blank_line(self):
        w = MarkdownWriter()
        w.append("Hello world")
        assert not w.can_commit()
        assert "Hello world" in w.render_partial().markup

    def test_single_paragraph_commits_after_blank_line(self):
        w = MarkdownWriter()
        w.append("Hello world\n\n")
        assert w.can_commit()
        assert "Hello world" in w.render_committed().markup

    def test_two_paragraphs_commits_first(self):
        w = MarkdownWriter()
        w.append("First paragraph\n\nSecond paragraph")
        assert w.can_commit()
        committed = w.render_committed()
        assert "First paragraph" in committed.markup

    def test_unclosed_fence_keeps_partial(self):
        w = MarkdownWriter()
        w.append("Text\n\n```python\ncode here")
        assert w.can_commit()
        assert "Text" in w.render_committed().markup
        assert "code here" in self._render_text(w)

    def test_closed_fence_commits_all(self):
        w = MarkdownWriter()
        w.append("Text\n\n```python\ncode here\n```")
        assert w.can_commit()

    def test_commit_clears_committed(self):
        w = MarkdownWriter()
        w.append("Block one\n\nBlock two")
        w.commit()
        assert not w.can_commit() or w.has_content()

    def test_flush_renders_everything(self):
        w = MarkdownWriter()
        w.append("Partial content")
        result = w.flush()
        assert result is not None
        assert not w.has_content()

    def test_flush_returns_none_when_empty(self):
        w = MarkdownWriter()
        assert w.flush() is None

    def test_streaming_code_block(self):
        w = MarkdownWriter()
        w.append("Here is code:\n\n")
        assert w.can_commit()
        w.commit()
        w.append("```python\n")
        assert not w.can_commit()
        w.append("def hello():\n    pass\n")
        assert not w.can_commit()
        w.append("```\n\nDone!")
        assert w.can_commit()

    def test_unclosed_code_block_renders_each_streamed_update(self):
        w = MarkdownWriter()
        w.append("```python\n")

        w.append("def greet")
        assert "def greet" in self._render_text(w)

        w.append("(name):\n")
        assert "def greet(name):" in self._render_text(w)

        w.append('    return f"Hello, {name}"')
        rendered = self._render_text(w)
        assert "def greet(name):" in rendered
        assert 'return f"Hello, {name}"' in rendered

    def test_blank_line_inside_fence_is_not_a_commit_boundary(self):
        w = MarkdownWriter()
        code = "```python\ndef first():\n    return 1\n\ndef second():\n    return 2\n```"

        w.append(code)

        assert not w.can_commit()
        rendered = self._render_text(w)
        assert "def first():" in rendered
        assert "def second():" in rendered

        w.append("\n\nAfter code")

        assert w.can_commit()
        assert w.render_committed().markup == code
        assert "After code" in w.render_partial().markup

    def test_code_highlighting_does_not_set_background_color(self):
        from rich.console import Console

        w = MarkdownWriter()
        w.append("```python\ndef greet():\n    return 42")

        segments = list(Console(width=80, force_terminal=True, color_system="standard").render(w.render_partial()))
        code_segments = [segment for segment in segments if segment.text.strip()]

        assert any(segment.style and segment.style.color is not None for segment in code_segments)
        assert all(segment.style is None or segment.style.bgcolor is None for segment in code_segments)

    def test_reset(self):
        w = MarkdownWriter()
        w.append("hello\n\nworld")
        w.reset()
        assert not w.has_content()
        assert not w.can_commit()

    def test_protocol_compliance(self):
        assert isinstance(MarkdownWriter(), StreamWriter)

    def test_row_count_empty(self):
        w = MarkdownWriter()
        from rich.text import Text

        assert w.row_count(Text(""), 80) == 0


class TestStreamPrinterIntegration:
    @pytest.mark.asyncio
    async def test_no_commit_per_token_without_block_boundary(self, monkeypatch):
        import asyncio
        from io import StringIO

        from rich.console import Console

        async def fake_run_in_terminal(func, render_cli_done=True):
            if asyncio.iscoroutine(func):
                await func
            elif callable(func):
                func()
            return None

        monkeypatch.setattr("bub.channels.cli.run_in_terminal", fake_run_in_terminal)

        console = Console(file=StringIO(), width=80, force_terminal=True, color_system=None)
        printer = _StreamPrinter(
            console=console,
            print_head=lambda: None,
            expand_thinking=False,
        )
        commits = 0
        real_commit = printer._writer.commit

        def spy_commit():
            nonlocal commits
            committed = real_commit()
            if committed:
                commits += 1
            return committed

        printer._writer.commit = spy_commit

        for ch in "春风一夜入江城":
            await printer.render(StreamEvent("text", {"delta": ch}))
        await printer.render(StreamEvent("final", {}))

        assert commits == 0
