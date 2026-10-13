"""Agent loop over tools, a system prompt and a tape store, calling the model given to each run."""

from __future__ import annotations

import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from functools import cached_property
from json import JSONDecodeError
from typing import Any, TypeVar, cast

import republic
from loguru import logger
from republic.events import Completed, ReasoningDelta, RefusalDelta, TextDelta, ToolCallReady

from bub.core.context import default_tape_context
from bub.core.events import (
    AfterLlmCall,
    AgentEvent,
    BeforeLlmCall,
    ChatCompleted,
    EventBus,
    LlmCallRequest,
    LlmCallResult,
    RunStarted,
    StepFinished,
    StepStarted,
    TapeEvent,
)
from bub.core.model import Model, ModelClient, is_context_length_error, model_client
from bub.core.recorder import TapeRecorder
from bub.core.sidecars import TapeSidecar
from bub.core.store import AsyncTapeStore, AsyncTapeStoreAdapter, InMemoryTapeStore, TapeStore, is_async_tape_store
from bub.core.system_prompt import PromptContext, PromptSection, SystemPrompt
from bub.core.tape import Tape, TapeContext
from bub.core.tools import Tool, ToolContext, ToolExecutor, model_tools
from bub.errors import BubError, ErrorKind
from bub.prompt import UserContent
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tracing import Span, current_span
from bub.turn import TurnState

MAX_AUTO_HANDOFF_RETRIES = 1

E = TypeVar("E", bound=AgentEvent)
T = TypeVar("T", bound=TapeEvent)
S = TypeVar("S", bound=PromptSection)


class Agent:
    """Run the agent loop: call the model, execute its tool calls, repeat until it answers.

    The agent owns no configuration, model or session: each run is given the model to
    call, a ``republic.ChatModel`` or a :class:`~bub.core.model.ModelClient`, and its
    generation options. The agent is assembled from its components:

    Args:
        store: Tape store holding conversations; each run works on one named tape in it.
            A function returning the store (or None) is called when the agent first uses
            its tapes. Defaults to an in-memory store.
        tools: Tools the model may call, kept by name in ``tools``.
        system_prompt: System prompt sections: text, or functions of the step's
            :class:`PromptContext` (prompt, tools, tape, state, agent) returning text.
            Add more with :meth:`add_system_prompt`.
        context: How a tape's entries become model context; defaults to
            :func:`default_tape_context`, which replays the recorded chat.
        sidecars: Sidecar capabilities mounted on every tape.
        max_steps: Maximum model calls in one run.
        name: Agent name, reported in ``invoke_agent`` traces.

    The loop reports its progress as events (:mod:`bub.core.events`) on ``events``;
    register handlers with ``agent.events.on``. A :class:`TapeRecorder`, registered first,
    records them on the run's tape. ``BeforeLlmCall`` and ``BeforeToolCall`` handlers
    may change or short-circuit the call.

    Subclasses customize a run by overriding :meth:`prepare_tools`,
    :meth:`pending_messages`, :meth:`has_pending_messages` and :meth:`continue_prompt`,
    each of which receives the run's tape.
    """

    def __init__(
        self,
        *,
        store: TapeStore | AsyncTapeStore | Callable[[], TapeStore | AsyncTapeStore | None] | None = None,
        tools: Iterable[Tool] = (),
        system_prompt: SystemPrompt | PromptSection | Iterable[PromptSection] = (),
        context: TapeContext | None = None,
        sidecars: Sequence[TapeSidecar] = (),
        max_steps: int = sys.maxsize,
        name: str = "agent",
    ) -> None:
        self._store = store
        self.tools = {tool.name: tool for tool in tools}
        self.system_prompt = SystemPrompt.of(system_prompt)
        self._context = context or default_tape_context()
        self._sidecars = tuple(sidecars)
        self.max_steps = max_steps
        self.name = name
        self.events = EventBus()
        TapeRecorder().subscribe(self.events)

    def add_system_prompt(self, section: S) -> S:
        """Append a system prompt section and return it, so this also works as a decorator::

        @agent.add_system_prompt
        def tools(context: PromptContext) -> str: ...
        """
        return self.system_prompt.add(section)

    def remove_system_prompt(self, section: PromptSection) -> None:
        """Remove a system prompt section."""
        self.system_prompt.remove(section)

    @cached_property
    def _tapes(self) -> Tape:
        """Unscoped tape over the agent's store, built on first use, when a store function is resolved."""
        store: TapeStore | AsyncTapeStore | None
        if callable(self._store) and not hasattr(self._store, "append"):
            store = self._store()
        else:
            store = cast("TapeStore | AsyncTapeStore | None", self._store)
        if store is None:
            store = InMemoryTapeStore()
        if not is_async_tape_store(store):
            store = AsyncTapeStoreAdapter(store)
        return Tape(store, self._context, sidecars=self._sidecars)

    def tape(self, name: str, *, state: TurnState | None = None) -> Tape:
        """Return tape ``name`` in the agent's store, with ``state`` as its turn state.

        Use it outside runs, for example to read a conversation or add an anchor; it
        writes to the store directly. Runs work on a fork instead (see :meth:`session`).
        """
        tapes = self._tapes
        return tapes.scoped(name, context=replace(tapes.context, state={} if state is None else state))

    @asynccontextmanager
    async def session(
        self, name: str, *, state: TurnState | None = None, merge_back: bool = True
    ) -> AsyncIterator[Tape]:
        """Open a fork of tape ``name`` for one run, with ``state`` as its turn state.

        The fork merges back on exit unless ``merge_back`` is false.
        """
        async with self.tape(name, state=state).fork_tape(merge_back=merge_back) as tape:
            await tape.ensure_bootstrap_anchor()
            yield tape

    async def run(
        self,
        prompt: list[UserContent],
        *,
        model: Model,
        options: Mapping[str, Any] | None = None,
        tape: str,
        state: TurnState | None = None,
        merge_back: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncStreamEvents:
        """Start a run of ``model`` on tape ``tape`` and return its stream; consume it to drive the loop.

        ``options`` are ``republic.ChatOptions`` (such as ``max_tokens`` or ``temperature``)
        sent with every model call of the run.

        The run works on a fork of the tape (see :meth:`session`) that stays open until
        the stream closes, even if it is never consumed. ``state`` is the run's mutable
        turn state, carried by events and tools; ``metadata`` is attached to the
        ``RunStarted`` event. Each model step ends with a ``final`` event; the run ends
        when a step makes no tool calls and no messages are pending. A context-length
        error triggers one automatic tape handoff and a retry of the prompt. An empty
        prompt yields an error without opening the tape. The run is traced as an
        ``invoke_agent`` span.
        """
        span = Span(
            f"invoke_agent {self.name}",
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": self.name,
                "gen_ai.conversation.id": (state or {}).get("session_id", tape),
            },
        )
        stack = AsyncExitStack()
        try:
            with span.activate():
                span.messages("gen_ai.input.messages", [republic.user(*prompt)])
                if not any(prompt):
                    events = events_from([
                        StreamEvent("text", {"delta": "error: empty prompt"}),
                        StreamEvent("final", {"text": "error: empty prompt", "ok": False}),
                    ])
                else:
                    session = await stack.enter_async_context(self.session(tape, state=state, merge_back=merge_back))
                    events = await self.run_on(session, prompt, model=model, options=options, metadata=metadata)
                    stack.push_async_callback(events.aclose)
        except BaseException as exc:
            span.fail(exc)
            try:
                with span.activate():
                    await stack.aclose()
            finally:
                span.end()
            raise
        return AsyncStreamEvents(_trace_events(events, span), state=events._state, on_close=stack.aclose, span=span)

    async def run_on(
        self,
        tape: Tape,
        prompt: list[UserContent],
        *,
        model: Model,
        options: Mapping[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncStreamEvents:
        """Start a run of ``model`` on an already open tape; the caller owns the tape's lifecycle."""
        client = model_client(model)
        await self._emit(RunStarted, tape, model=client.spec, prompt=prompt, metadata=dict(metadata or {}))
        state = StreamState()
        return AsyncStreamEvents(self._run_steps(tape, client, dict(options or {}), prompt, state), state=state)

    def run_once(
        self,
        prompt: list[UserContent] | None,
        *,
        model: Model,
        options: Mapping[str, Any] | None = None,
        tape: Tape,
    ) -> AsyncStreamEvents:
        """Make one call to ``model`` on an open tape and execute the tool calls it returns.

        ``prompt`` is the new user message; ``None`` continues the existing conversation.
        """
        state = StreamState()
        return AsyncStreamEvents(
            self._run_once(tape, model_client(model), dict(options or {}), prompt, state), state=state
        )

    async def prepare_tools(self, tape: Tape, prompt: list[UserContent] | None) -> list[Tool]:
        """Return the model-facing tools for the next model call."""
        return model_tools(self.tools.values())

    async def pending_messages(self, tape: Tape) -> list[list[UserContent]]:
        """Return user messages that arrived during the run, sent before the next prompt."""
        return []

    def has_pending_messages(self, tape: Tape) -> bool:
        """Return whether messages are waiting, which continues the run for another step."""
        return False

    async def continue_prompt(
        self, tape: Tape, prompt: list[UserContent] | None, state: StreamState
    ) -> list[UserContent] | None:
        """Return a user message for the next step, or None to continue the conversation as is."""
        return None

    async def _emit(self, event_type: type[T], tape: Tape, **fields: Any) -> T:
        return await self.events.emit(event_type(state=tape.context.state, tape=tape, **fields))

    async def _run_steps(
        self, tape: Tape, client: ModelClient, options: dict[str, Any], prompt: list[UserContent], state: StreamState
    ) -> AsyncGenerator[StreamEvent, None]:
        auto_handoff_remaining = MAX_AUTO_HANDOFF_RETRIES
        next_prompt: list[UserContent] | None = prompt
        for step in range(1, self.max_steps + 1):
            start = time.monotonic()
            should_continue = False
            logger.info("loop.step step={} tape={} model={}", step, tape.name, client.spec)
            await self._emit(StepStarted, tape, step=step, prompt=next_prompt)
            try:
                output = self.run_once(next_prompt, model=client, options=options, tape=tape)
                async with aclosing(output):
                    async for event in output:
                        yield event
                        if event.kind == "error":
                            await self._record_step(tape, step, start, "error", error=event.data.get("message", ""))
                        elif event.kind == "final":
                            should_continue = bool(event.data.get("tool_calls") or event.data.get("tool_results"))
            except Exception as exc:
                error_message = f"{exc!s}"
                if auto_handoff_remaining > 0 and is_context_length_error(error_message):
                    auto_handoff_remaining -= 1
                    logger.warning(
                        "auto_handoff: context length exceeded, performing automatic handoff. tape={} step={}",
                        tape.name,
                        step,
                    )
                    await tape.handoff(
                        name="auto_handoff/context_overflow",
                        state={"reason": "context_length_exceeded", "error": error_message},
                    )
                    await self._record_step(tape, step, start, "auto_handoff", error=error_message)
                    next_prompt = prompt
                    continue
                await self._record_step(tape, step, start, "error", error=error_message)
                raise

            state.error = output.error
            state.usage = output.usage
            if not (should_continue or self.has_pending_messages(tape)):
                await self._record_step(tape, step, start, "ok")
                return

            next_prompt = await self.continue_prompt(tape, next_prompt, state)
            await self._record_step(tape, step, start, "continue")

        raise RuntimeError(f"max_steps_reached={self.max_steps}")

    async def _record_step(self, tape: Tape, step: int, start: float, status: str, *, error: str | None = None) -> None:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await self._emit(StepFinished, tape, step=step, status=status, elapsed_ms=elapsed_ms, error=error)

    async def _run_once(
        self,
        tape: Tape,
        client: ModelClient,
        options: dict[str, Any],
        prompt: list[UserContent] | None,
        state: StreamState,
    ) -> AsyncGenerator[StreamEvent, None]:
        turn_state = tape.context.state
        tools = await self.prepare_tools(tape, prompt)
        prompt_context = PromptContext(agent=self, tape=tape, prompt=prompt or [], tools=tools, model=client.spec)
        system_prompt = await self.system_prompt.render(prompt_context) or None
        if (span := current_span()) and span.recording:
            span.set(**{"gen_ai.tool.definitions": [tool.to_schema() | {"type": "function"} for tool in tools]})
        pending = await self.pending_messages(tape)

        run_id = generate_run_id()
        messages, new_messages = await self._build_messages(
            tape, model=client.spec, run_id=run_id, system_prompt=system_prompt, prompt=prompt, pending=pending
        )
        request = LlmCallRequest(
            run_id=run_id,
            model=client.spec,
            messages=messages,
            tool_names=tuple(tool_item.name for tool_item in tools),
            max_tokens=options.get("max_tokens", client.max_tokens),
        )
        before = await self._emit(BeforeLlmCall, tape, request=request)
        request, decision = before.request, before.decision
        if decision is not None:
            await self._emit(
                ChatCompleted,
                tape,
                run_id=run_id,
                model=request.model,
                system_prompt=system_prompt,
                new_messages=new_messages,
                response_text=decision.text,
            )
            yield StreamEvent("text", {"delta": decision.text})
            yield StreamEvent("final", {"ok": True, "text": decision.text})
            return
        completion_started = time.monotonic()
        async with self._traced_completion(tape, client, options, request, tools, state) as (
            provider,
            completion,
            events,
        ):
            async for event in events:
                yield event
            response = completion.response
            completion_elapsed = time.monotonic() - completion_started
        text = response.text or response.refusal or ""

        yield StreamEvent("usage", {"usage": state.usage, "elapsed_seconds": completion_elapsed})

        tool_calls = response.tool_calls
        if tool_calls:
            tool_map = {tool_item.name: tool_item for tool_item in tools}
            serialized_tool_calls = [asdict(tool_call) for tool_call in tool_calls]
            tool_invocations = [_tool_invocation(tool_call, tool_map) for tool_call in tool_calls]
            yield StreamEvent("tool_call", {"tool_calls": serialized_tool_calls})
            turn_state["_runtime_tool_names"] = tuple(tool_map)
            context = ToolContext(tape=tape, run_id=run_id, state=turn_state, events=self.events)
            execution = await ToolExecutor(events=self.events).execute_async(
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
        await self._emit(
            ChatCompleted,
            tape,
            run_id=run_id,
            model=request.model,
            system_prompt=system_prompt,
            new_messages=new_messages,
            response_text=(text or None) if tool_calls else text,
            tool_calls=serialized_tool_calls,
            tool_results=tool_results,
            usage=state.usage,
            provider=provider,
            assistant_fields=assistant_fields,
        )
        if tool_calls:
            yield StreamEvent("tool_result", {"tool_results": tool_results})
            yield StreamEvent("final", {"ok": True, "tool_calls": serialized_tool_calls, "tool_results": tool_results})
        else:
            yield StreamEvent("final", {"ok": True, "text": text})

    async def _build_messages(
        self,
        tape: Tape,
        *,
        model: str,
        run_id: str,
        system_prompt: str | None,
        prompt: list[UserContent] | None,
        pending: list[list[UserContent]],
    ) -> tuple[list[republic.Message], list[republic.Message]]:
        try:
            messages = await tape.read_messages()
        except BubError as exc:
            await self._emit(
                ChatCompleted,
                tape,
                run_id=run_id,
                model=model,
                system_prompt=system_prompt,
                new_messages=[],
                response_text=None,
                error=exc,
                context_error=exc,
            )
            raise
        contents = [*pending, *([] if prompt is None else [prompt])]
        new_messages = [republic.user(*content) for content in contents]
        if system_prompt:
            messages = [republic.system(system_prompt), *messages]
        messages.extend(new_messages)
        return messages, new_messages

    @asynccontextmanager
    async def _traced_completion(
        self,
        tape: Tape,
        client: ModelClient,
        options: dict[str, Any],
        request: LlmCallRequest,
        tools: list[Tool],
        state: StreamState,
    ) -> AsyncIterator[tuple[str, republic.Stream[Any], AsyncIterator[StreamEvent]]]:
        provider, _, model_id = request.model.partition(":")
        span = Span(
            f"chat {model_id or request.model}",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model_id or request.model,
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
            call_options = {
                **options,
                "tools": [republic.Tool(item.name, item.description, item.parameters) for item in tools],
            }
            if request.max_tokens is not None:
                call_options["max_tokens"] = request.max_tokens
            async with AsyncExitStack() as stack:
                with span.activate():
                    provider, stream = await stack.enter_async_context(
                        client.open_stream(list(request.messages), spec=request.model, options=call_options)
                    )
                events = await stack.enter_async_context(aclosing(_stream_events(stream, span, state, text, calls)))
                yield provider, stream, events
        except BaseException as exc:
            span.fail(exc)
            if isinstance(exc, Exception):
                await self._fire_after_llm_call(tape, request, "".join(text), calls, state, started, error=exc)
            raise
        else:
            completed = stream.response
            await self._fire_after_llm_call(
                tape, request, completed.text or completed.refusal or "", completed.tool_calls, state, started
            )
        finally:
            usage = state.usage or {}
            span.set(**{
                "gen_ai.usage.input_tokens": usage.get("input_tokens"),
                "gen_ai.usage.output_tokens": usage.get("output_tokens"),
            })
            span.messages(
                "gen_ai.output.messages",
                [{"role": "assistant", "content": "".join(text), "tool_calls": [asdict(call) for call in calls]}],
            )
            span.end()

    async def _fire_after_llm_call(
        self,
        tape: Tape,
        request: LlmCallRequest,
        text: str,
        tool_calls: list[republic.ToolCall],
        state: StreamState,
        started: datetime,
        error: Exception | None = None,
    ) -> None:
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        result = LlmCallResult(
            run_id=request.run_id,
            text=text or None,
            tool_calls=[asdict(call) for call in tool_calls],
            usage=state.usage,
            error=error,
            duration_ms=duration_ms,
        )
        await self._emit(AfterLlmCall, tape, request=request, result=result)


def events_from(items: Iterable[StreamEvent]) -> AsyncStreamEvents:
    """Stream fixed events."""

    async def generator() -> AsyncIterator[StreamEvent]:
        for item in items:
            yield item

    return AsyncStreamEvents(generator())


async def _trace_events(events: AsyncStreamEvents, span: Span) -> AsyncGenerator[StreamEvent, None]:
    messages: list[dict[str, Any]] = []
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    try:
        async with aclosing(events):
            async for event in events:
                if span.recording:
                    if event.kind == "text":
                        text.append(str(event.data.get("delta", "")))
                    elif event.kind == "tool_call":
                        calls = event.data.get("tool_calls", [])
                        messages.append({"role": "assistant", "content": "".join(text), "tool_calls": calls})
                        text.clear()
                    elif event.kind == "tool_result":
                        for call, result in zip(calls, event.data.get("tool_results", []), strict=False):
                            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                    elif event.kind == "error":
                        span.fail(RuntimeError(str(event.data.get("message", "agent failed"))))
                yield event
    finally:
        if text:
            messages.append({"role": "assistant", "content": "".join(text)})
        span.messages("gen_ai.output.messages", messages)


def generate_run_id() -> str:
    return f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"


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


def _tool_invocation(tool_call: republic.ToolCall, tool_map: dict[str, Tool]) -> tuple[Tool, dict[str, Any]]:
    """Resolve a model tool call to (runtime tool, arguments).

    An unknown tool name is not treated as a fatal error: it is surfaced as a
    placeholder ``Tool`` so the invocation flows through ``ToolExecutor`` and
    ``BeforeToolCall`` handlers can recover it into a guidance
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
