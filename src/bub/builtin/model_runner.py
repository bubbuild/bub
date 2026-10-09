"""Republic requests and model-output helpers for the builtin agent."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from json import JSONDecodeError
from time import monotonic
from typing import Any

import republic
from loguru import logger
from republic.events import Completed, ReasoningDelta, RefusalDelta, TextDelta, ToolCallReady

from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import (
    AgentHooks,
    LlmCallDecision,
    LlmCallRequest,
    LlmCallResult,
)
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tape import Tape
from bub.tools import Tool, ToolContext, ToolExecutor, render_result
from bub.tracing import Span, current_span, event

CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)


class ModelRunner:
    def __init__(self, settings: AgentSettings, hooks: AgentHooks | None = None) -> None:
        self.settings = settings
        self.hooks = hooks

    @asynccontextmanager
    async def _completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[Tool],
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[tuple[ModelCandidate, republic.Stream[Any]]]:
        completion_error: Exception | None = None
        candidates = self.settings.model_candidates(model)
        for index, candidate in enumerate(candidates):
            stream: republic.Stream[Any] | None = None
            try:
                client_kwargs = self.settings.model_client_kwargs(candidate.provider_name or candidate.provider)
                chat_model = republic.get_model(f"{candidate.provider}:{candidate.model_id}", **client_kwargs)
                if span := current_span():
                    span.rename(f"chat {candidate.model_id}")
                    span.set(**{
                        "gen_ai.provider.name": candidate.provider,
                        "gen_ai.request.model": candidate.model_id,
                    })
                options = self._chat_options(candidate.provider, tools, max_tokens, reasoning_effort)
                stream = chat_model.stream(
                    _request_messages(messages, candidate.provider_name or candidate.provider, candidate.model_id),
                    **options,
                )
                await stream.__aenter__()
            except Exception as exc:
                if stream is not None:
                    await stream.__aexit__(type(exc), exc, exc.__traceback__)
                event("bub.model.attempt_failed", model=candidate.name, error=type(exc).__name__)
                if completion_error is None:
                    completion_error = exc
                if index == len(candidates) - 1:
                    raise completion_error from None
                logger.warning(
                    "model candidate failed; trying fallback model={} error={}", candidate.name, type(exc).__name__
                )

            else:
                try:
                    yield candidate, stream
                finally:
                    await stream.__aexit__(None, None, None)
                return

        raise RuntimeError("no model candidates available")

    def _chat_options(
        self, provider: str, tools: list[Tool], max_tokens: int | None, reasoning_effort: str | None
    ) -> dict[str, Any]:
        options = dict(self.settings.completion_args)
        if provider == "anthropic":
            options["extra_body"] = {"cache_control": {"type": "ephemeral"}} | options.get("extra_body", {})
        options["tools"] = [republic.Tool(item.name, item.description, item.parameters) for item in tools]
        if provider != "codex":
            options["max_tokens"] = max_tokens if max_tokens is not None else self.settings.max_tokens
        if reasoning_effort is not None:
            options["reasoning_effort"] = reasoning_effort
        return options

    def run(
        self,
        *,
        tape: Tape,
        model: str,
        tools: list[Tool],
        system_prompt: str | None,
        prompt: str | list[dict] | None,
        steering_messages: list[list[dict[str, Any]] | str] | None = None,
    ) -> AsyncStreamEvents:
        state = StreamState()

        async def iterator() -> AsyncGenerator[StreamEvent, None]:
            run_id = self.generate_run_id()
            messages, new_messages = await self.build_messages(
                tape=tape,
                run_id=run_id,
                system_prompt=system_prompt,
                prompt=prompt,
                model=model,
                steering_messages=steering_messages,
            )
            request = LlmCallRequest(
                run_id=run_id,
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
                    run_id=run_id,
                    system_prompt=system_prompt,
                    new_messages=new_messages,
                    response_text=decision.text,
                    model=request.model,
                )
                yield StreamEvent("text", {"delta": decision.text})
                yield StreamEvent("final", {"ok": True, "text": decision.text})
                return
            completion_started = monotonic()
            async with self._traced_completion(request, tools, tape, state) as (candidate, completion, events):
                async for event in events:
                    yield event
                response = completion.response
                completion_elapsed = monotonic() - completion_started
            text = response.text or response.refusal or ""
            provider = candidate.provider_name or candidate.provider

            yield StreamEvent("usage", {"usage": state.usage, "elapsed_seconds": completion_elapsed})

            tool_calls = response.tool_calls
            if tool_calls:
                tool_map = {tool_item.name: tool_item for tool_item in tools}
                serialized_tool_calls = [asdict(tool_call) for tool_call in tool_calls]
                tool_invocations = [_tool_invocation(tool_call, tool_map) for tool_call in tool_calls]
                yield StreamEvent("tool_call", {"tool_calls": serialized_tool_calls})
                tape.context.state["_runtime_tool_names"] = tuple(tool_map)
                context = ToolContext(tape=tape, run_id=run_id, state=tape.context.state)
                execution = await ToolExecutor(hooks=self.hooks).execute_async(
                    tool_invocations,
                    context=context,
                    call_ids=[call.id for call in tool_calls],
                )
                tool_results = execution.tool_results
            else:
                serialized_tool_calls, tool_results = [], None
            assistant_fields = {
                key: value
                for key, value in {
                    "reasoning": response.reasoning,
                    "provider_data": [
                        asdict(part) for part in response.message.parts if isinstance(part, republic.ProviderData)
                    ],
                }.items()
                if value
            }
            if assistant_fields or any(call.metadata for call in response.tool_calls):
                assistant_fields.update(source_provider=provider, source_model=candidate.model_id)
            await tape.record_chat(
                run_id=run_id,
                system_prompt=system_prompt,
                new_messages=new_messages,
                response_text=(text or None) if tool_calls else text,
                tool_calls=serialized_tool_calls,
                tool_results=tool_results,
                model=request.model,
                usage=state.usage,
                assistant_fields=assistant_fields,
                provider=provider,
            )
            if tool_calls:
                yield StreamEvent("tool_result", {"tool_results": tool_results})
                yield StreamEvent(
                    "final", {"ok": True, "tool_calls": serialized_tool_calls, "tool_results": tool_results}
                )
            else:
                yield StreamEvent("final", {"ok": True, "text": text})

        return AsyncStreamEvents(iterator(), state=state)

    @asynccontextmanager
    async def _traced_completion(
        self,
        request: LlmCallRequest,
        tools: list[Tool],
        tape: Tape,
        state: StreamState,
    ) -> AsyncIterator[tuple[ModelCandidate, republic.Stream[Any], AsyncIterator[StreamEvent]]]:
        provider, _, model = request.model.partition(":")
        span = Span(
            f"chat {model or request.model}",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model or request.model,
                "gen_ai.request.max_tokens": request.max_tokens,
                "gen_ai.conversation.id": tape.context.state.get("session_id"),
                "bub.run_id": request.run_id,
                "bub.tape": tape.name,
            },
        )
        span.messages("gen_ai.input.messages", request.messages)
        if span.recording:
            span.set(**{"gen_ai.tool.definitions": [tool.to_schema() | {"type": "function"} for tool in tools]})
        started = datetime.now(UTC)
        # Partial output for hook errors and cancellation traces.
        text: list[str] = []
        calls: list[republic.ToolCall] = []

        try:
            async with asyncio.timeout(self.settings.model_timeout_seconds), AsyncExitStack() as stack:
                with span.activate():
                    candidate, stream = await stack.enter_async_context(
                        self._completion(
                            model=request.model,
                            messages=list(request.messages),
                            tools=tools,
                            max_tokens=request.max_tokens,
                            reasoning_effort=tape.context.state.get("reasoning_effort"),
                        )
                    )
                events = await stack.enter_async_context(aclosing(_stream_events(stream, span, state, text, calls)))
                yield candidate, stream, events
        except BaseException as exc:
            span.fail(exc)
            if isinstance(exc, Exception):
                await self._fire_after_llm_call(request, "".join(text), calls, state, started, tape, error=exc)
            raise
        else:
            completed = stream.response
            await self._fire_after_llm_call(
                request, completed.text or completed.refusal or "", completed.tool_calls, state, started, tape
            )
        finally:
            usage = state.usage or {}
            span.set(**{
                "gen_ai.usage.input_tokens": usage.get("input_tokens"),
                "gen_ai.usage.output_tokens": usage.get("output_tokens"),
            })
            span.messages(
                "gen_ai.output.messages",
                [
                    {
                        "role": "assistant",
                        "content": "".join(text),
                        "tool_calls": [asdict(call) for call in calls],
                    }
                ],
            )
            span.end()

    @staticmethod
    def generate_run_id() -> str:
        return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"

    async def _fire_after_llm_call(
        self,
        request: LlmCallRequest,
        text: str,
        tool_calls: list[republic.ToolCall],
        state: StreamState,
        started: datetime,
        tape: Tape,
        error: Exception | None = None,
    ) -> None:
        if self.hooks is None:
            return
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        result = LlmCallResult(
            run_id=request.run_id,
            text=text or None,
            tool_calls=[asdict(call) for call in tool_calls],
            usage=state.usage,
            error=error,
            duration_ms=duration_ms,
        )
        await self.hooks.after_llm_call(request, result, state=tape.context.state)

    async def build_messages(
        self,
        *,
        tape: Tape,
        run_id: str,
        system_prompt: str | None,
        prompt: str | list[dict] | None,
        model: str,
        steering_messages: list[list[dict[str, Any]] | str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            messages = await tape.read_messages()
        except BubError as exc:
            await tape.record_chat(
                run_id=run_id,
                system_prompt=system_prompt,
                context_error=exc,
                new_messages=[],
                response_text=None,
                error=exc,
                model=model,
            )
            raise
        steering = [{"role": "user", "content": message} for message in (steering_messages or [])]
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}, *messages]
        new_messages = [*steering]
        if prompt is not None:
            new_messages.append({"role": "user", "content": prompt})
        messages.extend(new_messages)
        return messages, new_messages


async def _stream_events(
    stream: republic.Stream[Any],
    span: Span,
    state: StreamState,
    text: list[str],
    calls: list[republic.ToolCall],
) -> AsyncGenerator[StreamEvent, None]:
    native_events = aiter(stream)
    while True:
        with span.activate():
            try:
                item = await anext(native_events)
            except StopAsyncIteration:
                break
        match item:
            case TextDelta(chunk=delta) | RefusalDelta(chunk=delta):
                text.append(delta)
                yield StreamEvent("text", {"delta": delta})
            case ReasoningDelta(chunk=delta):
                yield StreamEvent("reasoning", {"delta": delta})
            case ToolCallReady(call=call):
                calls.append(call)
            case Completed(response=completed):
                state.usage = {
                    **asdict(completed.token_usage),
                    "total_tokens": completed.token_usage.total_tokens,
                }
                span.set(**{
                    "gen_ai.response.model": completed.model,
                    "gen_ai.response.id": completed.id,
                    "gen_ai.response.finish_reasons": [completed.finish_reason] if completed.finish_reason else None,
                })


def _request_messages(messages: list[dict[str, Any]], provider: str, model: str) -> list[republic.Message]:
    """Convert the tape and hook dictionaries at the request boundary."""
    result: list[republic.Message] = []
    calls: dict[str, republic.ToolCall] = {}
    for message in messages:
        role = message["role"]
        if role not in {"system", "user", "assistant", "tool"}:
            raise BubError(ErrorKind.INVALID_INPUT, f"Unknown message role: {role}")
        if role == "tool":
            result.append(
                republic.Message(
                    "tool",
                    tool_results=(
                        republic.tool_result(
                            calls[message["tool_call_id"]],
                            render_result(message.get("content", "")),
                            is_error=bool(message.get("is_error")),
                        ),
                    ),
                )
            )
            continue
        converted = republic.user(*_request_content(message.get("content")))
        keep_metadata = message.get("source_provider") == provider and message.get("source_model") == model
        parts = list(converted.parts)
        if keep_metadata:
            if reasoning := message.get("reasoning"):
                parts.append(republic.Reasoning(reasoning))
            parts.extend(republic.ProviderData(**item) for item in message.get("provider_data", []))
        tool_calls = tuple(
            republic.ToolCall(**(call | {"metadata": call.get("metadata", {}) if keep_metadata else {}}))
            for call in message.get("tool_calls") or []
        )
        calls.update((call.id, call) for call in tool_calls)
        result.append(replace(converted, role=role, parts=tuple(parts), tool_calls=tool_calls))
    return result


def _request_content(content: object) -> list[str | republic.Image | republic.Audio | republic.Video]:
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise BubError(ErrorKind.INVALID_INPUT, "Expected text or a list of message content blocks.")
    result: list[str | republic.Image | republic.Audio | republic.Video] = []
    for block in content:
        match block:
            case {"type": "text", "text": str(text)}:
                result.append(text)
            case {"type": "image", "url": str(url), "media_type": str(mime)}:
                result.append(republic.image(url, media_type=mime))
            case {"type": "audio", "url": str(url), "media_type": str(mime)}:
                result.append(republic.audio(url, media_type=mime))
            case {"type": "video", "url": str(url), "media_type": str(mime)}:
                result.append(republic.video(url, media_type=mime))
            case _:
                raise BubError(
                    ErrorKind.INVALID_INPUT,
                    f"Unsupported message content block: {block.get('type') if isinstance(block, dict) else type(block).__name__}",
                )
    return result


def _tool_invocation(
    tool_call: republic.ToolCall,
    tool_map: dict[str, Tool],
) -> tuple[Tool, dict[str, Any]]:
    """Resolve a model tool call to (runtime tool, arguments).

    An unknown tool name is not treated as a fatal error: it is surfaced as a
    placeholder ``Tool`` so the invocation flows through ``ToolExecutor`` and
    builtin hooks (e.g. ``before_tool_call``) can recover it into a guidance
    ``tool_result`` instead of interrupting the turn. If no hook replaces the
    call, the placeholder raises a clear tool error rather than succeeding with
    an empty result.
    """
    tool_name = tool_call.name
    try:
        arguments = tool_call.args
    except JSONDecodeError as exc:
        raise BubError(ErrorKind.INVALID_INPUT, "Expected a function tool call with JSON object arguments.") from exc
    if not isinstance(arguments, dict):
        raise BubError(ErrorKind.INVALID_INPUT, "Expected a function tool call with JSON object arguments.")
    tool_obj = tool_map.get(tool_name)
    if tool_obj is None:

        def raise_unknown_tool(**_: Any) -> None:
            raise BubError(ErrorKind.TOOL, f"Unknown tool name: {tool_name}.")

        return Tool(name=tool_name, handler=raise_unknown_tool), arguments
    return tool_obj, arguments


def is_context_length_error(error_msg: str) -> bool:
    """Check whether an error message indicates a context-length / prompt-too-long failure."""
    return bool(CONTEXT_LENGTH_PATTERNS.search(error_msg))
