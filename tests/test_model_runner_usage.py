"""Regression: streaming completions must request token usage.

OpenAI-style streaming responses omit the `usage` block unless the request sets
`stream_options.include_usage`. Without it every streamed run records zero
tokens (and zero cost) in the tape. The field is only valid for OpenAI-compatible
providers, so it must be gated on the provider base class.
"""

from __future__ import annotations

import pytest
from any_llm import AnyLLM

from bub.builtin.model_runner import _stream_usage_options


def _provider(name: str) -> AnyLLM:
    return AnyLLM.create(name, api_key="test-key")


def test_openai_streaming_requests_usage() -> None:
    assert _stream_usage_options(_provider("openai"), stream=True) == {"include_usage": True}


def test_openai_compatible_provider_streaming_requests_usage() -> None:
    # openrouter (and other OpenAI-compatible providers) subclass BaseOpenAIProvider.
    assert _stream_usage_options(_provider("openrouter"), stream=True) == {"include_usage": True}


def test_non_streaming_does_not_set_options() -> None:
    # Non-streaming completions already carry usage in the response body.
    assert _stream_usage_options(_provider("openai"), stream=False) is None


def test_non_openai_provider_is_not_offered_the_field() -> None:
    # anthropic is not a BaseOpenAIProvider and rejects stream_options.
    assert _stream_usage_options(_provider("anthropic"), stream=True) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
