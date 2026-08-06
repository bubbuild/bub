from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from any_llm.constants import LLMProvider
from any_llm.providers.anthropic.base import BaseAnthropicProvider
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import ChatCompletionChunk

from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.builtin.tape import Tape
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeContext


class _FakeStreamingOpenAIProvider(BaseOpenAIProvider):
    SUPPORTS_COMPLETION_STREAMING = True

    def __init__(self) -> None:
        self.completion_kwargs: dict[str, Any] | None = None

    async def acompletion(self, **kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
        self.completion_kwargs = kwargs
        include_usage = kwargs.get("stream_options") == {"include_usage": True}

        async def stream() -> AsyncIterator[ChatCompletionChunk]:
            yield ChatCompletionChunk.model_validate({
                "id": "chatcmpl_test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {"role": "assistant", "content": "done"},
                    }
                ],
            })
            final_chunk: dict[str, Any] = {
                "id": "chatcmpl_test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-test",
                "choices": [],
            }
            if include_usage:
                final_chunk["usage"] = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
            yield ChatCompletionChunk.model_validate(final_chunk)

        return stream()


class _FakeStreamingAnthropicProvider(BaseAnthropicProvider):
    def __init__(self) -> None:
        self.completion_kwargs: dict[str, Any] | None = None

    def _init_client(self, api_key: str | None = None, api_base: str | None = None, **kwargs: Any) -> None:
        pass

    async def acompletion(self, **kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
        self.completion_kwargs = kwargs

        async def stream() -> AsyncIterator[ChatCompletionChunk]:
            if False:
                yield

        return stream()


class _FakeOpenAIModelRunner(ModelRunner):
    def __init__(self, settings: AgentSettings, llm: _FakeStreamingOpenAIProvider) -> None:
        super().__init__(settings)
        self._llm = llm

    def iter_llm_clients(self, model: str) -> Iterator[tuple[ModelCandidate, _FakeStreamingOpenAIProvider]]:
        yield ModelCandidate(provider=LLMProvider.OPENAI, model_id=model, name=f"openai:{model}"), self._llm


class _FakeAnthropicModelRunner(ModelRunner):
    def __init__(self, settings: AgentSettings, llm: _FakeStreamingAnthropicProvider) -> None:
        super().__init__(settings)
        self._llm = llm

    def iter_llm_clients(self, model: str) -> Iterator[tuple[ModelCandidate, _FakeStreamingAnthropicProvider]]:
        yield ModelCandidate(provider=LLMProvider.ANTHROPIC, model_id=model, name=f"anthropic:{model}"), self._llm


@pytest.mark.asyncio
async def test_streaming_openai_usage_is_requested_and_recorded_in_tape(tmp_path: Path) -> None:
    store = InMemoryTapeStore()
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(store), TapeContext()).scoped("test-tape")
    llm = _FakeStreamingOpenAIProvider()
    runner = _FakeOpenAIModelRunner(
        AgentSettings.model_construct(model="openai:gpt-test", max_tokens=100, model_timeout_seconds=None),
        llm,
    )

    await tape.ensure_bootstrap_anchor()
    events = [
        event async for event in runner.run(tape=tape, model="gpt-test", tools=[], system_prompt=None, prompt="hello")
    ]

    assert llm.completion_kwargs is not None
    assert llm.completion_kwargs["stream"] is True
    assert llm.completion_kwargs["stream_options"] == {"include_usage": True}
    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": "done"}),
        ("final", {"ok": True, "text": "done"}),
    ]
    run_events = [
        entry for entry in store.read("test-tape") or [] if entry.kind == "event" and entry.payload.get("name") == "run"
    ]
    assert len(run_events) == 1
    assert run_events[0].payload["data"]["usage"] == {
        "completion_tokens": 2,
        "prompt_tokens": 3,
        "total_tokens": 5,
    }


@pytest.mark.asyncio
async def test_anthropic_prompt_caching_is_requested() -> None:
    llm = _FakeStreamingAnthropicProvider()
    runner = _FakeAnthropicModelRunner(
        AgentSettings.model_construct(model="anthropic:claude-test", max_tokens=100),
        llm,
    )

    await runner.completion_response(model="claude-test", messages=[{"role": "user", "content": "hello"}], tools=[])

    assert llm.completion_kwargs is not None
    assert llm.completion_kwargs["stream"] is True
    assert llm.completion_kwargs["cache_control"] == {"type": "ephemeral"}
    assert "stream_options" not in llm.completion_kwargs


@pytest.mark.asyncio
async def test_completion_args_are_forwarded_without_overriding_managed_args() -> None:
    llm = _FakeStreamingOpenAIProvider()
    runner = _FakeOpenAIModelRunner(
        AgentSettings.model_construct(
            model="openai:gpt-test",
            max_tokens=100,
            completion_args={
                "reasoning_effort": "high",
                "model": "ignored-model",
                "max_tokens": 1,
                "stream": False,
                "stream_options": {"include_usage": False},
            },
        ),
        llm,
    )

    await runner.completion_response(
        model="gpt-test",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        max_tokens=42,
    )

    assert llm.completion_kwargs is not None
    assert llm.completion_kwargs["reasoning_effort"] == "high"
    assert llm.completion_kwargs["model"] == "gpt-test"
    assert llm.completion_kwargs["max_tokens"] == 42
    assert llm.completion_kwargs["stream"] is True
    assert llm.completion_kwargs["stream_options"] == {"include_usage": True}
