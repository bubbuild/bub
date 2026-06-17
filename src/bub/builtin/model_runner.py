"""LLM completion and model-output helpers for the builtin agent."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal, cast

from any_llm import AnyLLM
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
from pydantic import TypeAdapter, ValidationError

from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.builtin.tape import Tape
from bub.runtime import BubError, ErrorKind, StreamEvent, StreamState
from bub.tools import Tool

CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)
TOOL_ARGUMENTS_ADAPTER = TypeAdapter(dict[str, Any])
CompletionResult = ChatCompletion | ParsedChatCompletion[Any] | AsyncIterator[ChatCompletionChunk]


class ModelRunner:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings

    def iter_llm_clients(self, model: str) -> Iterator[tuple[ModelCandidate, AnyLLM]]:
        for candidate in self.settings.model_candidates(model):
            yield (
                candidate,
                AnyLLM.create(
                    candidate.provider,
                    **self.settings.model_client_kwargs(candidate.provider),
                ),
            )

    async def completion_response(
        self, *, model: str, messages: list[dict[str, Any]], tools: list[Tool]
    ) -> CompletionResult:
        from bub.builtin.tools import completion_tools

        tool_payloads = completion_tools(tools) or None
        completion_messages: list[dict[str, Any] | ChatCompletionMessage] = list(messages)
        clients = list(self.iter_llm_clients(model))
        completion_error: Exception | None = None
        for index, (candidate, llm) in enumerate(clients):
            try:
                return await llm.acompletion(
                    model=candidate.model_id,
                    messages=completion_messages,
                    tools=tool_payloads,
                    max_tokens=self.settings.max_tokens,
                    stream=llm.SUPPORTS_COMPLETION_STREAMING,
                )
            except Exception as exc:
                if completion_error is None:
                    completion_error = exc
                if index == len(clients) - 1:
                    raise completion_error from None
                logger.warning("model candidate failed; trying fallback model={} error={}", candidate.name, exc)

        raise RuntimeError("no model candidates available")

    async def run(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        state: StreamState,
        output: ModelOutputAccumulator,
    ) -> AsyncGenerator[StreamEvent, None]:
        completion = await self.completion_response(model=model, messages=messages, tools=tools)
        async for event in self._completion_events(completion, state, output):
            yield event

    async def _completion_events(
        self,
        completion: CompletionResult,
        state: StreamState,
        output: ModelOutputAccumulator,
    ) -> AsyncGenerator[StreamEvent, None]:
        if isinstance(completion, ChatCompletion):
            if usage := Tape._extract_usage(completion):
                state.usage = usage
            output.response = completion
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
        if usage := Tape._extract_usage(chunk):
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
        self.response: ChatCompletion | ParsedChatCompletion[Any] | None = None
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
) -> tuple[Tool, dict[str, Any]]:
    tool_name, arguments = parse_native_function_call(tool_call)
    tool_obj = tool_map.get(tool_name)
    if tool_obj is None:
        raise BubError(ErrorKind.TOOL, f"Unknown tool name: {tool_name}.")
    return tool_obj, arguments


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
