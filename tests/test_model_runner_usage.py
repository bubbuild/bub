"""Regression: streaming completions must request token usage — but only from
providers that accept the field.

OpenAI-style streaming responses omit the `usage` block unless the request sets
`stream_options.include_usage`; without it every streamed run records zero
tokens. The field is only valid for OpenAI-compatible providers, so it must be
gated on the provider base class — passing it to e.g. anthropic would break the
request. This test guards that gate (the non-obvious part); the trivial
"streaming openai gets the field" path is covered implicitly.
"""

from __future__ import annotations

import pytest
from any_llm import AnyLLM

from bub.builtin.model_runner import _stream_usage_options


@pytest.mark.parametrize(
    ("provider", "stream", "expected"),
    [
        ("openai", True, {"include_usage": True}),  # primary path: usage must be requested
        ("openai", False, None),  # non-streaming already carries usage in the body
        ("anthropic", True, None),  # not OpenAI-compatible: must not receive the field
    ],
)
def test_stream_usage_options_gate(provider: str, stream: bool, expected: dict | None) -> None:
    llm = AnyLLM.create(provider, api_key="test-key")
    assert _stream_usage_options(llm, stream=stream) == expected
