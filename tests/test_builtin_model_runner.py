from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from any_llm.constants import LLMProvider
from any_llm.providers.anthropic.base import BaseAnthropicProvider
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import ChatCompletionChunk, ChatCompletionMessageFunctionToolCall, Function

from bub.builtin.model_runner import ModelRunner, _adapt_messages_for_provider, tool_invocation_from_native
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.store import InMemoryTapeStore, TapeQuery
from bub.tape import Tape, TapeContext, bub_event_type, event_data
from bub.tools import ToolExecutor


@pytest.mark.parametrize("provider", [LLMProvider.GEMINI, LLMProvider.VERTEXAI])
def test_adapt_messages_converts_video_url_for_google_providers(provider: LLMProvider) -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this video"},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,dmlkZW8="}},
                {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "ogg"}},
            ],
        }
    ]

    result = _adapt_messages_for_provider(messages, provider)

    assert result == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this video"},
                {"type": "file", "file": {"file_data": "data:video/mp4;base64,dmlkZW8="}},
                {"type": "file", "file": {"file_data": "data:audio/ogg;base64,YXVkaW8="}},
            ],
        }
    ]
    assert messages[0]["content"][1]["type"] == "video_url"


def test_adapt_messages_keeps_native_multimodal_blocks_for_openrouter() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,dmlkZW8="}},
                {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "ogg"}},
            ],
        }
    ]

    assert _adapt_messages_for_provider(messages, LLMProvider.OPENROUTER) is messages


@pytest.mark.asyncio
async def test_unknown_tool_placeholder_surfaces_error_without_hooks() -> None:
    tool_call = ChatCompletionMessageFunctionToolCall(
        id="call-1",
        type="function",
        function=Function(name="missing_tool", arguments="{}"),
    )
    invocation = tool_invocation_from_native(tool_call, {})

    execution = await ToolExecutor().execute_async([invocation])

    assert invocation.tool_call_id == "call-1"
    assert execution.error is not None
    assert "missing_tool" in execution.error.message


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
    tape = Tape(store, TapeContext()).scoped("test-tape")
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
    model_messages = [
        record
        async for record in store.scan(TapeQuery("test-tape").types(bub_event_type("message")))
        if event_data(record.event).get("source") == "agent"
    ]
    assert len(model_messages) == 1
    assert event_data(model_messages[0].event)["metrics"] == {
        "completion_tokens": 2,
        "prompt_tokens": 3,
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
async def test_run_applies_reasoning_effort_from_tape_state(tmp_path: Path) -> None:
    tape = Tape(
        InMemoryTapeStore(),
        TapeContext(state={"reasoning_effort": "high"}),
    ).scoped("test-tape")
    llm = _FakeStreamingOpenAIProvider()
    runner = _FakeOpenAIModelRunner(
        AgentSettings.model_construct(
            model="openai:gpt-test",
            max_tokens=100,
            model_timeout_seconds=None,
            completion_args={"reasoning_effort": "low"},
        ),
        llm,
    )

    await tape.ensure_bootstrap_anchor()
    events = runner.run(tape=tape, model="gpt-test", tools=[], system_prompt=None, prompt="hello")
    [event async for event in events]

    assert llm.completion_kwargs is not None
    assert llm.completion_kwargs["reasoning_effort"] == "high"


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
