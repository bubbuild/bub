from bub.channels.telegram import _truncate_reply_content


def test_truncate_reply_content_keeps_short_text():
    assert _truncate_reply_content("hello") == "hello"


def test_truncate_reply_content_caps_long_text():
    text = "x" * 1200
    result = _truncate_reply_content(text, max_chars=1000)
    assert result.startswith("x" * 1000)
    assert "[truncated 200 chars]" in result
    assert len(result) < len(text)
