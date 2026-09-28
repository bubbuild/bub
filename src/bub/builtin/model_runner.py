"""LLM completion and model-output helpers for the builtin agent."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from bub.builtin.model_provider import ModelOutput, Provider, completion_events, create_provider
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.errors import BubError
from bub.hooks.interception import AgentHooks, LlmCallDecision, LlmCallRequest, LlmCallResult
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tape import Tape
from bub.tools import Tool, ToolContext, ToolExecutor
from bub.tracing import Span

CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)


class ModelRunner:
    def __init__(self, settings: AgentSettings, hooks: AgentHooks | None = None) -> None:
        self.settings = settings
        self.hooks = hooks

    async def create_provider(self, candidate: ModelCandidate) -> Provider:
        """Create one adapter per attempt; override to borrow an explicit client."""
        return await create_provider(self.settings, candidate)

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
            output = ModelOutput()
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
                await self.record_chat(
                    tape=tape,
                    run_id=run_id,
                    system_prompt=system_prompt,
                    new_messages=new_messages,
                    response_text=decision.text,
                    model=request.model,
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

            try:
                completion_started = monotonic()
                async with aclosing(self._traced_completion(request, tools, tape, state, output)) as events:
                    async for event in events:
                        yield event
                completion_elapsed = monotonic() - completion_started
            except Exception as exc:
                # Cancellation / consumer close (BaseException) intentionally
                # bypasses after_llm_call: only real completions and failures
                # are terminal observations.
                await fire_after(exc)
                raise
            await fire_after(output.failure)

            yield StreamEvent("usage", {"usage": state.usage, "elapsed_seconds": completion_elapsed})

            if output.failure is not None:
                state.error = output.failure
                await self.record_chat(
                    tape=tape,
                    run_id=run_id,
                    system_prompt=system_prompt,
                    new_messages=new_messages,
                    response_text=output.text,
                    response=output.response,
                    model=request.model,
                    usage=state.usage,
                    response_message=output.native_message,
                    response_context=False,
                    finish_reason=output.finish_reason,
                    error=output.failure,
                )
                yield StreamEvent("error", {"message": str(output.failure)})
                yield StreamEvent("final", {"ok": False, "text": output.text, "finish_reason": output.finish_reason})
                return

            serialized_tool_calls = output.serialized_tool_calls
            if serialized_tool_calls:
                tool_map = {tool_item.name: tool_item for tool_item in tools}
                tool_invocations = output.invocations(tool_map)
                yield StreamEvent("tool_call", {"tool_calls": serialized_tool_calls})
                context = ToolContext(tape=tape, run_id=run_id, state=tape.context.state)
                execution = await ToolExecutor(hooks=self.hooks).execute_async(
                    tool_invocations,
                    context=context,
                    call_ids=[call["id"] for call in serialized_tool_calls],
                )
                result_messages = output.result_messages(execution)
                await self.record_chat(
                    tape=tape,
                    run_id=run_id,
                    system_prompt=system_prompt,
                    new_messages=new_messages,
                    response_text=output.text or None,
                    response_message=output.native_message,
                    result_messages=result_messages,
                    finish_reason=output.finish_reason,
                    tool_calls=serialized_tool_calls,
                    tool_results=execution.tool_results,
                    response=output.response,
                    model=request.model,
                    usage=state.usage,
                )
                yield StreamEvent("tool_result", {"tool_results": execution.tool_results})
                yield StreamEvent(
                    "final", {"ok": True, "tool_calls": serialized_tool_calls, "tool_results": execution.tool_results}
                )
                return

            text = output.text
            await self.record_chat(
                tape=tape,
                run_id=run_id,
                system_prompt=system_prompt,
                new_messages=new_messages,
                response_text=text,
                response_message=output.native_message,
                finish_reason=output.finish_reason,
                response=output.response,
                model=request.model,
                usage=state.usage,
            )
            yield StreamEvent("final", {"ok": True, "text": text})

        return AsyncStreamEvents(iterator(), state=state)

    def _traced_completion(
        self,
        request: LlmCallRequest,
        tools: list[Tool],
        tape: Tape,
        state: StreamState,
        output: ModelOutput,
    ) -> AsyncStreamEvents:
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
            span.set(**{
                "gen_ai.tool.definitions": [tool.to_schema()["function"] | {"type": "function"} for tool in tools]
            })

        async def iterator() -> AsyncGenerator[StreamEvent, None]:
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                async with aclosing(completion_events(self, request, tools, tape, state, output)) as source:
                    async for item in source:
                        yield item

        async def finish() -> None:
            if not span.recording:
                return
            usage = state.usage or {}
            span.set(**{
                "gen_ai.usage.input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                "gen_ai.usage.output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
            })
            span.messages(
                "gen_ai.output.messages",
                [
                    {
                        "role": "assistant",
                        "content": output.text,
                        "tool_calls": output.serialized_tool_calls,
                    }
                ],
            )

        return AsyncStreamEvents(iterator(), state=state, span=span, on_close=finish)

    @staticmethod
    def generate_run_id() -> str:
        return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"

    async def _fire_after_llm_call(
        self,
        request: LlmCallRequest,
        output: ModelOutput,
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
            text=output.text or None,
            tool_calls=output.serialized_tool_calls,
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
            await self.record_context_error(
                tape=tape,
                run_id=run_id,
                system_prompt=system_prompt,
                error=exc,
                model=model,
            )
            raise
        steering_messages_native = [{"role": "user", "content": message} for message in (steering_messages or [])]
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}, *messages]
        new_messages = [*steering_messages_native]
        if prompt is not None:
            new_messages.append({"role": "user", "content": prompt})
        messages.extend(new_messages)
        return messages, new_messages

    async def record_context_error(
        self,
        *,
        tape: Tape,
        run_id: str,
        system_prompt: str | None,
        error: BubError,
        model: str,
    ) -> None:
        await self.record_chat(
            tape=tape,
            run_id=run_id,
            system_prompt=system_prompt,
            context_error=error,
            new_messages=[],
            response_text=None,
            error=error,
            model=model,
        )

    async def record_chat(
        self,
        *,
        tape: Tape,
        run_id: str,
        system_prompt: str | None,
        new_messages: list[dict[str, Any]],
        response_text: str | None,
        context_error: BubError | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_results: list[Any] | None = None,
        error: BubError | None = None,
        response: Any | None = None,
        provider: str | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
        response_message: dict[str, Any] | None = None,
        result_messages: list[dict[str, Any]] | None = None,
        response_context: bool = True,
        finish_reason: str | None = None,
    ) -> None:
        native: dict[str, Any] = {
            key: value
            for key, value in {
                "response_message": response_message,
                "result_messages": result_messages,
                "finish_reason": finish_reason,
            }.items()
            if value is not None
        }
        if not response_context:
            native["response_context"] = False
        await tape.record_chat(
            run_id=run_id,
            system_prompt=system_prompt,
            new_messages=new_messages,
            response_text=response_text,
            context_error=context_error,
            tool_calls=tool_calls,
            tool_results=tool_results,
            error=error,
            response=response,
            provider=provider,
            model=model,
            usage=usage,
            **native,
        )


def is_context_length_error(error_msg: str) -> bool:
    """Check whether an error message indicates a context-length / prompt-too-long failure."""
    return bool(CONTEXT_LENGTH_PATTERNS.search(error_msg))
