from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from any_llm.constants import LLMProvider
from any_llm.providers.anthropic.base import BaseAnthropicProvider
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageFunctionToolCall,
    Function,
)
from openai.types.chat.chat_completion_message_custom_tool_call import ChatCompletionMessageCustomToolCall, Custom

from bub.builtin.context import default_tape_context
from bub.builtin.model_runner import (
    ModelRunner,
    _adapt_messages_for_provider,
    parse_native_function_call,
    tool_invocation_from_native,
)
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.errors import BubError, ErrorKind
from bub.store import FileTapeStore
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, Tape, TapeContext
from bub.tools import Tool, ToolExecutor


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

    assert execution.error is not None
    assert "missing_tool" in execution.error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("content", [None, "", "Check byte equality.\nOnly exact equality counts."])
@pytest.mark.parametrize("continuation_prompt", ["Continue.", "", None])
async def test_tool_call_text_survives_into_next_request_after_tape_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    content: str | None,
    continuation_prompt: str | None,
) -> None:
    calls = [
        {"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for name in ("inspect", "compare")
    ]
    response = BaseOpenAIProvider._convert_completion_response({
        "id": "completion-1",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": content, "tool_calls": calls},
            }
        ],
    })

    async def stream() -> AsyncIterator[ChatCompletionChunk]:
        deltas = [{"content": part} for part in (content or "").splitlines(keepends=True)]
        deltas.append({"tool_calls": [{"index": index, **call} for index, call in enumerate(calls)]})
        for delta in deltas:
            yield ChatCompletionChunk.model_validate({
                "id": "completion-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-model",
                "choices": [{"index": 0, "delta": delta}],
            })

    requests: list[list[dict[str, Any]]] = []

    async def complete(**kwargs: Any) -> ChatCompletion | AsyncIterator[ChatCompletionChunk]:
        requests.append(kwargs["messages"])
        return stream() if streaming else response

    runner = ModelRunner(AgentSettings.model_construct(model="test-model", model_timeout_seconds=None))
    monkeypatch.setattr(runner, "completion_response", complete)
    tools = [Tool(name="inspect", handler=lambda: "files found"), Tool(name="compare", handler=lambda: "bytes differ")]
    root = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped("test-tape")
    async with root.fork_tape() as tape:
        await tape.ensure_bootstrap_anchor()
        events = [
            event
            async for event in runner.run(
                tape=tape, model="test-model", tools=tools, system_prompt=None, prompt="Compare the outputs."
            )
        ]

    reloaded = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped(
        "test-tape"
    )
    async for _ in runner.run(
        tape=reloaded, model="test-model", tools=tools, system_prompt=None, prompt=continuation_prompt
    ):
        pass

    assert "".join(event.data["delta"] for event in events if event.kind == "text") == (content or "")
    expected_messages = [
        {"role": "user", "content": "Compare the outputs."},
        {"role": "assistant", "content": content or "", "tool_calls": calls},
        {"role": "tool", "content": "files found", "tool_call_id": "call-inspect", "name": "inspect"},
        {"role": "tool", "content": "bytes differ", "tool_call_id": "call-compare", "name": "compare"},
    ]
    if continuation_prompt is not None:
        expected_messages.append({"role": "user", "content": continuation_prompt})
    assert requests[1] == expected_messages
    if continuation_prompt is None:
        persisted = await reloaded.store.fetch_all(reloaded.query().kinds("message"))
        assert [entry.payload for entry in persisted if entry.payload.get("role") == "user"] == [
            {"role": "user", "content": "Compare the outputs."}
        ]


@pytest.mark.asyncio
async def test_build_messages_keeps_steering_when_continuation_has_no_prompt(tmp_path: Path) -> None:
    runner = ModelRunner(AgentSettings.model_construct(model="test-model"))
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), default_tape_context()).scoped("steering")
    await tape.ensure_bootstrap_anchor()

    messages, new_messages = await runner.build_messages(
        tape=tape,
        run_id="run-1",
        system_prompt=None,
        prompt=None,
        model="test-model",
        steering_messages=["new user direction"],
    )

    assert messages == new_messages == [{"role": "user", "content": "new user direction"}]


@pytest.mark.parametrize("arguments", ["[]", "null", "1", "not json"])
def test_function_tool_call_rejects_non_object_arguments(arguments: str) -> None:
    call = ChatCompletionMessageFunctionToolCall(
        id="call-1", type="function", function=Function(name="inspect", arguments=arguments)
    )

    with pytest.raises(BubError) as exc_info:
        parse_native_function_call(call)

    assert exc_info.value.kind == ErrorKind.INVALID_INPUT


def test_custom_tool_call_is_not_treated_as_a_function_call() -> None:
    call = ChatCompletionMessageCustomToolCall(
        id="call-1", type="custom", custom=Custom(name="inspect", input="raw input")
    )

    with pytest.raises(BubError) as exc_info:
        parse_native_function_call(call)

    assert exc_info.value.kind == ErrorKind.INVALID_INPUT


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
async def test_streaming_openai_usage_is_requested_and_recorded_in_tape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter([10.0, 12.0])
    monkeypatch.setattr("bub.builtin.model_runner.monotonic", lambda: next(clock))
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
        (
            "usage",
            {
                "usage": {"completion_tokens": 2, "prompt_tokens": 3, "total_tokens": 5},
                "elapsed_seconds": 2.0,
            },
        ),
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
async def test_run_applies_reasoning_effort_from_tape_state(tmp_path: Path) -> None:
    tape = Tape(
        tmp_path,
        AsyncTapeStoreAdapter(InMemoryTapeStore()),
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
