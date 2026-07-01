from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from any_llm.constants import LLMProvider
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


class _FakeOpenAIModelRunner(ModelRunner):
    def __init__(self, settings: AgentSettings, llm: _FakeStreamingOpenAIProvider) -> None:
        super().__init__(settings)
        self._llm = llm

    def iter_llm_clients(self, model: str) -> Iterator[tuple[ModelCandidate, _FakeStreamingOpenAIProvider]]:
        yield ModelCandidate(provider=LLMProvider.OPENAI, model_id=model, name=f"openai:{model}"), self._llm


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
