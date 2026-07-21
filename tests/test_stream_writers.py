"""Unit tests for streaming text writers."""

from __future__ import annotations

from bub.channels.cli.writers import (
    MarkdownWriter,
    PlainTextWriter,
    StreamWriter,
    _trim_partial_closing_fences,
)


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
    def test_single_paragraph_is_commits(self):
        w = MarkdownWriter()
        w.append("Hello world")
        # No unclosed fences → single paragraph is committable
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


class TestTrimPartialClosingFences:
    def test_strips_unclosed_fence(self):
        text = "```python\ncode\n```"
        result = _trim_partial_closing_fences(text)
        assert result.count("```") % 2 == 0

    def test_preserves_paired_fences(self):
        text = "```python\ncode\n```\n\n```bash\nmore"
        result = _trim_partial_closing_fences(text)
        assert result.count("```") == 2

    def test_no_fences_unchanged(self):
        text = "Just plain text"
        assert _trim_partial_closing_fences(text) == text

    def test_empty_string(self):
        assert _trim_partial_closing_fences("") == ""

    def test_single_open_fence(self):
        text = "```python\ndef hello():"
        result = _trim_partial_closing_fences(text)
        assert "```" not in result
