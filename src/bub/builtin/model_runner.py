"""LLM completion and model-output helpers for the builtin agent."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from any_llm import AnyLLM
from any_llm.constants import LLMProvider
from any_llm.providers.anthropic.base import BaseAnthropicProvider
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageToolCall,
    ChoiceDeltaToolCall,
    Function,
    ParsedChatCompletion,
)
from loguru import logger
from pydantic import BaseModel, TypeAdapter, ValidationError

from bub.builtin.codex_provider import OpenaiCodexProvider, should_use_openai_codex_provider
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.channels.message import audio_mime_type_from_format
from bub.errors import BubError, ErrorKind, error_payload, is_cancellation
from bub.hooks.interception import (
    AgentHooks,
    LlmCallDecision,
    LlmCallRequest,
    LlmCallResult,
)
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tape import Tape
from bub.tools import Tool, ToolContext, ToolExecutor, ToolInvocation

CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)
TOOL_ARGUMENTS_ADAPTER = TypeAdapter(dict[str, Any])
CompletionResult = ChatCompletion | ParsedChatCompletion[Any] | AsyncIterator[ChatCompletionChunk]
GOOGLE_FILE_CONTENT_PROVIDERS = frozenset({LLMProvider.GEMINI, LLMProvider.VERTEXAI})


def _extra_options(llm: AnyLLM, *, stream: bool) -> dict[str, Any]:
    """Return provider-specific extra completion options."""
    if isinstance(llm, BaseAnthropicProvider):
        return {"cache_control": {"type": "ephemeral"}}
    elif stream and isinstance(llm, BaseOpenAIProvider):
        return {"stream_options": {"include_usage": True}}
    return {}


def _usage_from_response(response: object) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if isinstance(usage, dict):
        return usage
    if isinstance(usage, BaseModel):
        payload = usage.model_dump(exclude_none=True)
        return payload if isinstance(payload, dict) else None
    return None


def _adapt_messages_for_provider(messages: list[dict[str, Any]], provider: LLMProvider) -> list[dict[str, Any]]:
    """Translate canonical multimodal blocks when a provider uses a different wire format."""
    if provider not in GOOGLE_FILE_CONTENT_PROVIDERS:
        return messages

    adapted_messages: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            adapted_messages.append(message)
            continue

        adapted_content: list[Any] = []
        changed = False
        for part in content:
            if not isinstance(part, dict):
                adapted_content.append(part)
                continue

            if part.get("type") == "video_url":
                video_url = part.get("video_url")
                url = video_url.get("url") if isinstance(video_url, dict) else None
                if isinstance(url, str) and url:
                    adapted_content.append({"type": "file", "file": {"file_data": url}})
                    changed = True
                    continue
            elif part.get("type") == "input_audio":
                input_audio = part.get("input_audio")
                data = input_audio.get("data") if isinstance(input_audio, dict) else None
                audio_format = input_audio.get("format") if isinstance(input_audio, dict) else None
                if isinstance(data, str) and data and isinstance(audio_format, str) and audio_format:
                    mime_type = audio_mime_type_from_format(audio_format)
                    file_data = f"data:{mime_type};base64,{data}"
                    adapted_content.append({"type": "file", "file": {"file_data": file_data}})
                    changed = True
                    continue
            adapted_content.append(part)

        adapted_messages.append({**message, "content": adapted_content} if changed else message)
    return adapted_messages


class ModelRunner:
    def __init__(
        self,
        settings: AgentSettings,
        hooks: AgentHooks | None = None,
    ) -> None:
        self.settings = settings
        self.hooks = hooks

    def iter_llm_clients(self, model: str) -> Iterator[tuple[ModelCandidate, AnyLLM]]:
        for candidate in self.settings.model_candidates(model):
            client_kwargs = self.settings.model_client_kwargs(candidate.provider)
            yield (
                candidate,
                self.create_llm_client(candidate, client_kwargs),
            )

    @staticmethod
    def create_llm_client(candidate: ModelCandidate, client_kwargs: dict[str, Any]) -> AnyLLM:
        if candidate.provider == LLMProvider.OPENAI and should_use_openai_codex_provider(
            candidate.provider.value,
            candidate.model_id,
            api_key=client_kwargs.get("api_key"),
            api_base=client_kwargs.get("api_base"),
        ):
            return OpenaiCodexProvider(**client_kwargs)
        return AnyLLM.create(candidate.provider, **client_kwargs)

    async def completion_response(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> CompletionResult:
        tool_payloads = [tool.to_schema() for tool in tools] or None
        clients = list(self.iter_llm_clients(model))
        completion_error: Exception | None = None
        for index, (candidate, llm) in enumerate(clients):
            try:
                streaming = llm.SUPPORTS_COMPLETION_STREAMING
                completion_messages = _adapt_messages_for_provider(messages, candidate.provider)
                completion_kwargs = {
                    **self.settings.completion_args,
                    **_extra_options(llm, stream=streaming),
                    "model": candidate.model_id,
                    "messages": completion_messages,
                    "tools": tool_payloads,
                    "max_tokens": max_tokens if max_tokens is not None else self.settings.max_tokens,
                    "stream": streaming,
                }
                if reasoning_effort is not None:
                    completion_kwargs["reasoning_effort"] = reasoning_effort
                return cast("CompletionResult", await llm.acompletion(**completion_kwargs))
            except Exception as exc:
                if completion_error is None:
                    completion_error = exc
                if index == len(clients) - 1:
                    raise completion_error from None
                logger.warning("model candidate failed; trying fallback model={} error={}", candidate.name, exc)

        raise RuntimeError("no model candidates available")

    def run(  # noqa: C901
        self,
        *,
        tape: Tape,
        model: str,
        tools: list[Tool],
        system_prompt: str | None,
        prompt: str | list[dict],
        steering_messages: list[list[dict[str, Any]] | str] | None = None,
    ) -> AsyncStreamEvents:
        state = StreamState()

        async def iterator() -> AsyncGenerator[StreamEvent, None]:
            model_call_id = self.generate_model_call_id()
            messages, new_messages = await self.build_messages(
                tape=tape,
                model_call_id=model_call_id,
                system_prompt=system_prompt,
                prompt=prompt,
                steering_messages=steering_messages,
            )
            output = ModelOutputAccumulator()
            request = LlmCallRequest(
                model_call_id=model_call_id,
                model=model,
                messages=messages,
                tool_names=tuple(tool_item.name for tool_item in tools),
                max_tokens=self.settings.max_tokens,
            )
            decision: LlmCallDecision | None = None
            if self.hooks is not None:
                request, decision = await self.hooks.before_llm_call(request, state=tape.context.state)
            if decision is not None:
                await tape.record_chat(
                    model_call_id=model_call_id,
                    system_prompt=system_prompt,
                    new_messages=new_messages,
                    response_text=decision.text,
                    llm_call_count=0,
                )
                yield StreamEvent("text", {"delta": decision.text})
                yield StreamEvent("final", {"ok": True, "text": decision.text})
                return
            llm_started = datetime.now(UTC)
            after_fired = False

            async def fire_after(error: Exception | None = None) -> None:
                """Fire after_llm_call once per completed call (success or Exception failure); cancellation/consumer close bypasses it."""

                nonlocal after_fired
                if after_fired:
                    return
                after_fired = True
                await self._fire_after_llm_call(request, output, state, llm_started, tape, error=error)

            provider, separator, requested_model = request.model.partition(":")
            operation_started = time.monotonic()
            await tape.record_operation(
                "model",
                "started",
                {
                    "provider": provider if separator else "custom",
                    "model": requested_model if separator else request.model,
                },
                model_call_id=model_call_id,
            )
            try:
                async with asyncio.timeout(self.settings.model_timeout_seconds):
                    completion = await self.completion_response(
                        model=request.model,
                        messages=list(request.messages),
                        tools=tools,
                        max_tokens=request.max_tokens,
                        reasoning_effort=tape.context.state.get("reasoning_effort"),
                    )
                    async for event in self._completion_events(completion, state, output):
                        yield event
            except Exception as exc:
                await tape.record_operation(
                    "model",
                    "failed",
                    {"duration_ms": int((time.monotonic() - operation_started) * 1000), "error": error_payload(exc)},
                    model_call_id=model_call_id,
                )
                await fire_after(exc)
                raise
            except BaseException as exc:
                await tape.record_operation(
                    "model",
                    "cancelled" if is_cancellation(exc) else "failed",
                    {"duration_ms": int((time.monotonic() - operation_started) * 1000), "error": error_payload(exc)},
                    model_call_id=model_call_id,
                )
                raise
            else:
                await tape.record_operation(
                    "model",
                    "completed",
                    {"duration_ms": int((time.monotonic() - operation_started) * 1000), "usage": state.usage},
                    model_call_id=model_call_id,
                )
            await fire_after()

            tool_calls = output.tool_calls
            if tool_calls:
                tool_map = {tool_item.name: tool_item for tool_item in tools}
                serialized_tool_calls = [tool_call.model_dump(exclude_none=True) for tool_call in tool_calls]
                tool_invocations = [tool_invocation_from_native(tool_call, tool_map) for tool_call in tool_calls]
                yield StreamEvent("tool_call", {"tool_calls": serialized_tool_calls})
                context = ToolContext(tape=tape, model_call_id=model_call_id, state=tape.context.state)
                execution = await ToolExecutor(hooks=self.hooks).execute_async(
                    tool_invocations,
                    context=context,
                )
                await tape.record_chat(
                    model_call_id=model_call_id,
                    system_prompt=system_prompt,
                    new_messages=new_messages,
                    response_text=None,
                    tool_calls=serialized_tool_calls,
                    tool_results=execution.tool_results,
                    model=request.model,
                    usage=state.usage,
                )
                yield StreamEvent("tool_result", {"tool_results": execution.tool_results})
                yield StreamEvent(
                    "final", {"ok": True, "tool_calls": serialized_tool_calls, "tool_results": execution.tool_results}
                )
                return

            text = output.text
            await tape.record_chat(
                model_call_id=model_call_id,
                system_prompt=system_prompt,
                new_messages=new_messages,
                response_text=text,
                model=request.model,
                usage=state.usage,
            )
            yield StreamEvent("final", {"ok": True, "text": text})

        return AsyncStreamEvents(iterator(), state=state)

    @staticmethod
    def generate_model_call_id() -> str:
        return f"model-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"

    async def _fire_after_llm_call(
        self,
        request: LlmCallRequest,
        output: ModelOutputAccumulator,
        state: StreamState,
        started: datetime,
        tape: Tape,
        error: Exception | None = None,
    ) -> None:
        if self.hooks is None:
            return
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        result = LlmCallResult(
            model_call_id=request.model_call_id,
            text=output.text or None,
            tool_calls=[call.model_dump(exclude_none=True) for call in output.tool_calls],
            usage=state.usage,
            error=error,
            duration_ms=duration_ms,
        )
        await self.hooks.after_llm_call(request, result, state=tape.context.state)

    async def build_messages(
        self,
        *,
        tape: Tape,
        model_call_id: str,
        system_prompt: str | None,
        prompt: str | list[dict],
        steering_messages: list[list[dict[str, Any]] | str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        prompt_message: dict[str, Any] = {"role": "user", "content": prompt}
        try:
            messages = await tape.read_messages()
        except BubError as exc:
            await tape.record_chat(
                model_call_id=model_call_id,
                system_prompt=system_prompt,
                context_error=exc,
                new_messages=[],
                response_text=None,
                error=exc,
            )
            raise
        steering_messages_native = [{"role": "user", "content": message} for message in (steering_messages or [])]
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}, *messages]
        new_messages = [*steering_messages_native, prompt_message]
        messages.extend(new_messages)
        return messages, new_messages

    async def _completion_events(
        self,
        completion: CompletionResult,
        state: StreamState,
        output: ModelOutputAccumulator,
    ) -> AsyncGenerator[StreamEvent, None]:
        if isinstance(completion, ChatCompletion):
            if usage := _usage_from_response(completion):
                state.usage = usage
            message = completion.choices[0].message
            for event in self._completion_message_events(message, output):
                yield event
            return

        async for chunk in completion:
            async for event in self._completion_chunk_events(chunk, state, output):
                yield event

    def _completion_message_events(
        self,
        message: ChatCompletionMessage,
        output: ModelOutputAccumulator,
    ) -> Iterable[StreamEvent]:
        if message.reasoning:
            yield StreamEvent("reasoning", {"delta": self.reasoning_text(message.reasoning)})
        if message.content:
            output.add_text(message.content)
            yield StreamEvent("text", {"delta": message.content})
        output.add_message_tool_calls(cast("Iterable[ChatCompletionMessageToolCall]", message.tool_calls or []))

    async def _completion_chunk_events(
        self,
        chunk: ChatCompletionChunk,
        state: StreamState,
        output: ModelOutputAccumulator,
    ) -> AsyncGenerator[StreamEvent, None]:
        if usage := _usage_from_response(chunk):
            state.usage = usage
        for choice in chunk.choices:
            delta = choice.delta
            if delta.reasoning:
                yield StreamEvent("reasoning", {"delta": self.reasoning_text(delta.reasoning)})
            if delta.content:
                output.add_text(delta.content)
                yield StreamEvent("text", {"delta": delta.content})
            if delta.tool_calls:
                output.merge_delta_tool_calls(delta.tool_calls)

    @staticmethod
    def reasoning_text(reasoning: object) -> str:
        content = getattr(reasoning, "content", reasoning)
        return "" if content is None else str(content)


@dataclass
class StreamToolCall:
    id: str | None = None
    type: Literal["function"] | None = None
    name: str | None = None
    arguments: str = ""

    def merge(self, delta: ChoiceDeltaToolCall) -> None:
        if delta.id:
            self.id = delta.id
        if delta.type:
            self.type = delta.type
        if delta.function is None:
            return
        if delta.function.name:
            if self.name is None or self.name == delta.function.name:
                self.name = delta.function.name
            else:
                self.name += delta.function.name
        if delta.function.arguments:
            self.arguments += delta.function.arguments

    def as_tool_call(self, index: int) -> ChatCompletionMessageFunctionToolCall:
        return ChatCompletionMessageFunctionToolCall(
            id=self.id or f"call_{index}",
            type=self.type or "function",
            function=Function(name=self.name or "", arguments=self.arguments or "{}"),
        )


class ModelOutputAccumulator:
    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._message_calls: list[ChatCompletionMessageToolCall] = []
        self._stream_calls: dict[int, StreamToolCall] = {}

    def add_text(self, text: str) -> None:
        self._text_parts.append(text)

    def add_message_tool_calls(self, calls: Iterable[ChatCompletionMessageToolCall]) -> None:
        self._message_calls.extend(calls)

    def merge_delta_tool_calls(self, deltas: Iterable[ChoiceDeltaToolCall]) -> None:
        for delta in deltas:
            self._stream_calls.setdefault(delta.index, StreamToolCall()).merge(delta)

    @property
    def text(self) -> str:
        return "".join(self._text_parts)

    @property
    def tool_calls(self) -> list[ChatCompletionMessageToolCall]:
        if self._message_calls:
            return list(self._message_calls)
        return [self._stream_calls[index].as_tool_call(index) for index in sorted(self._stream_calls)]


def tool_invocation_from_native(
    tool_call: ChatCompletionMessageToolCall,
    tool_map: dict[str, Tool],
) -> ToolInvocation:
    """Resolve a model tool call while retaining its provider call identity.

    An unknown tool name is not treated as a fatal error: it is surfaced as a
    placeholder ``Tool`` so the invocation flows through ``ToolExecutor`` and
    builtin hooks (e.g. ``before_tool_call``) can recover it into a guidance
    ``tool_result`` instead of interrupting the turn. If no hook replaces the
    call, the placeholder raises a clear tool error rather than succeeding with
    an empty result.
    """
    tool_name, arguments = parse_native_function_call(tool_call)
    tool_obj = tool_map.get(tool_name)
    if tool_obj is None:

        def raise_unknown_tool(**_: Any) -> None:
            raise BubError(ErrorKind.TOOL, f"Unknown tool name: {tool_name}.")

        tool_obj = Tool(name=tool_name, handler=raise_unknown_tool)
    return ToolInvocation(tool=tool_obj, arguments=arguments, tool_call_id=tool_call.id)


def parse_native_function_call(tool_call: ChatCompletionMessageToolCall) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, ChatCompletionMessageFunctionToolCall):
        raise BubError(ErrorKind.INVALID_INPUT, "Expected a function tool call with JSON object arguments.")
    try:
        arguments = TOOL_ARGUMENTS_ADAPTER.validate_json(tool_call.function.arguments or "{}")
    except ValidationError as exc:
        raise BubError(ErrorKind.INVALID_INPUT, "Expected a function tool call with JSON object arguments.") from exc
    return tool_call.function.name, arguments


def is_context_length_error(error_msg: str) -> bool:
    """Check whether an error message indicates a context-length / prompt-too-long failure."""
    return bool(CONTEXT_LENGTH_PATTERNS.search(error_msg))
