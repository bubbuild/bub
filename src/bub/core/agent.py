"""Agent loop over tools, a system prompt and a tape store, calling its model or the one given to a run."""

from __future__ import annotations

import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any, TypeVar

import republic
from loguru import logger
from republic.events import Completed, ReasoningDelta, RefusalDelta, TextDelta, ToolCallReady
from republic.tools import BuiltinTool

from bub.core.commands import parse_command, parse_command_args, strip_command_prefix, validate_command_prefix
from bub.core.context import default_tape_context
from bub.core.events import (
    AgentEvent,
    ChatFinished,
    CommandFinished,
    EventBus,
    LLMCallFinished,
    LLMCallRequest,
    LLMCallResult,
    LLMCallStarted,
    RunFinished,
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


class Agent:
    """Run the agent loop: call the model, execute its tool calls, repeat until it answers.

    The agent owns no configuration or session. A run calls the model given to it, or
    else the agent's ``model``: a ``republic.ChatModel`` or a
    :class:`~bub.core.model.ModelClient`. The agent is assembled from its components:

    Args:
        model: Default model of runs not given one; None requires each run to give one.
        store: Tape store holding conversations; each run works on one named tape in it.
            Defaults to an in-memory store.
        tools: Tools the model may call. Bub tools are kept by name in ``tools``;
            ``republic.tools`` built-in tools, which the provider runs itself, are kept in
            ``builtin_tools`` and sent with every model call.
        system_prompt: System prompt sections: text, or functions of the step's
            :class:`PromptContext` (prompt, tools, tape, state, agent) returning text.
            Add more with ``agent.system_prompt.add``.
        context: How a tape's entries become model context; defaults to
            :func:`default_tape_context`, which replays the recorded chat.
        sidecars: Sidecar capabilities mounted on every tape.
        max_steps: Maximum model calls in one run.
        max_tool_concurrency: Maximum tool calls from one model response that run at
            once. None runs them all concurrently; 1 runs them one at a time, in order.
        name: Agent name, reported in ``invoke_agent`` traces.
        command_prefix: When set, a text prompt starting with it runs as a command
            (see :meth:`run_command`) instead of the agent loop. None disables commands.

    The loop reports its progress as events (:mod:`bub.core.events`) on ``events``;
    register handlers with ``agent.events.on``. A :class:`TapeRecorder`, registered first,
    records them on the run's tape. ``LLMCallStarted`` and ``ToolCallStarted`` handlers
    may change or short-circuit the call.

    Subclasses customize a run by overriding :meth:`prepare_tools`,
    :meth:`pending_messages`, :meth:`has_pending_messages` and :meth:`continue_prompt`,
    each of which receives the run's tape.
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        store: TapeStore | AsyncTapeStore | None = None,
        tools: Iterable[Tool | BuiltinTool] = (),
        system_prompt: SystemPrompt | PromptSection | Iterable[PromptSection] = (),
        context: TapeContext | None = None,
        sidecars: Sequence[TapeSidecar] = (),
        max_steps: int = sys.maxsize,
        max_tool_concurrency: int | None = None,
        name: str = "agent",
        command_prefix: str | None = None,
    ) -> None:
        self.model = model
        self.tools: dict[str, Tool] = {}
        self.builtin_tools: list[BuiltinTool] = []
        for tool in tools:
            if isinstance(tool, Tool):
                self.tools[tool.name] = tool
            else:
                self.builtin_tools.append(tool)
        self.system_prompt = SystemPrompt.of(system_prompt)
        if store is None:
            store = InMemoryTapeStore()
        self._tapes = Tape(
            store if is_async_tape_store(store) else AsyncTapeStoreAdapter(store),
            context or default_tape_context(),
            sidecars=tuple(sidecars),
        )
        self.max_steps = max_steps
        if max_tool_concurrency is not None and max_tool_concurrency < 1:
            raise ValueError("max_tool_concurrency must be a positive integer or None")
        self.max_tool_concurrency = max_tool_concurrency
        self.name = name
        self.command_prefix = None if command_prefix is None else validate_command_prefix(command_prefix)
        self.events = EventBus()
        TapeRecorder().subscribe(self.events)

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
    ) -> AsyncGenerator[Tape]:
        """Open a fork of tape ``name`` for one run, with ``state`` as its turn state.

        The fork merges back on exit unless ``merge_back`` is false.
        """
        async with self.tape(name, state=state).fork_tape(merge_back=merge_back) as tape:
            await tape.ensure_bootstrap_anchor()
            yield tape

    @asynccontextmanager
    async def run(
        self,
        prompt: str | list[UserContent],
        *,
        model: Model | None = None,
        options: Mapping[str, Any] | None = None,
        tape: str,
        state: TurnState | None = None,
        merge_back: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncGenerator[AsyncStreamEvents]:
        """Run ``model``, or the agent's model, on tape ``tape``; iterate the yielded stream to drive the loop.

        ::

            async with agent.run(prompt, tape="session") as stream:
                async for event in stream:
                    ...

        Leaving the block closes the stream, even after an early exit: the tape fork
        merges back and the trace span ends. See :meth:`start` for the parameters.
        """
        stream = await self.start(
            prompt, model=model, options=options, tape=tape, state=state, merge_back=merge_back, metadata=metadata
        )
        async with aclosing(stream):
            yield stream

    async def start(
        self,
        prompt: str | list[UserContent],
        *,
        model: Model | None = None,
        options: Mapping[str, Any] | None = None,
        tape: str,
        state: TurnState | None = None,
        merge_back: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncStreamEvents:
        """Start a run on tape ``tape`` and return its stream; the caller must close it.

        Prefer :meth:`run`; use this to hand the stream to code that consumes and closes it.

        The run calls ``model``, or the agent's ``model`` when it is None; a run that
        calls a model without either raises ``ValueError``.

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
        if isinstance(prompt, str):
            prompt = [prompt]
        try:
            with span.activate():
                span.messages("gen_ai.input.messages", [republic.user(*prompt)])
                if not any(prompt):
                    return _fixed_stream("error: empty prompt", ok=False, span=span, on_close=stack.aclose)
                session = await stack.enter_async_context(self.session(tape, state=state, merge_back=merge_back))
                return await self.run_on(
                    session, prompt, model=model, options=options, metadata=metadata, span=span, on_close=stack.aclose
                )
        except BaseException as exc:
            span.fail(exc)
            try:
                with span.activate():
                    await stack.aclose()
            finally:
                span.end()
            raise

    async def run_on(
        self,
        tape: Tape,
        prompt: list[UserContent],
        *,
        model: Model | None = None,
        options: Mapping[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        span: Span | None = None,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncStreamEvents:
        """Start a run on an already open tape; the caller owns the tape's lifecycle.

        A command prompt (see :meth:`command_line`) runs as a command instead, without calling the model.
        ``span`` traces the run's stream, and ``on_close`` runs after the stream closes.
        """
        if (line := self.command_line(prompt)) is not None:
            return _fixed_stream(await self.run_command(tape, line), ok=True, span=span, on_close=on_close)
        client = self._model_client(model)
        started = time.monotonic()
        await self._emit(RunStarted, tape, model=client.spec, prompt=prompt, metadata=dict(metadata or {}))
        state = StreamState()
        tracker = _RunTracker(span)

        async def close() -> None:
            try:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                await self._emit(
                    RunFinished,
                    tape,
                    status=tracker.status,
                    steps=tracker.steps,
                    elapsed_ms=elapsed_ms,
                    error=tracker.error,
                )
            finally:
                if on_close is not None:
                    await on_close()

        return AsyncStreamEvents(
            self._run_steps(tape, client, dict(options or {}), prompt, state, tracker),
            state=state,
            on_close=close,
            span=span,
        )

    async def run_once(
        self,
        prompt: list[UserContent] | None,
        *,
        model: Model | None = None,
        options: Mapping[str, Any] | None = None,
        tape: Tape,
        state: StreamState | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Make one call to ``model``, or the agent's model, on an open tape and execute the tool calls it returns.

        ``prompt`` is the new user message; ``None`` continues the existing conversation.

        ``state``, when given, receives the call's usage.
        """
        client = self._model_client(model)
        options = dict(options or {})
        if state is None:
            state = StreamState()
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
        request = LLMCallRequest(
            run_id=run_id,
            model=client.spec,
            messages=messages,
            tool_names=tuple(tool_item.name for tool_item in tools),
            max_tokens=options.get("max_tokens", client.max_tokens),
        )
        before = await self._emit(LLMCallStarted, tape, request=request)
        request, decision = before.request, before.decision
        if decision is not None:
            await self._emit(
                ChatFinished,
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
            execution = await ToolExecutor(events=self.events, max_concurrency=self.max_tool_concurrency).execute_many(
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
            ChatFinished,
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

    def _model_client(self, model: Model | None) -> ModelClient:
        if model is None:
            model = self.model
        if model is None:
            raise ValueError("no model to run: pass model to the run or to the agent")
        return model_client(model)

    def command_line(self, prompt: list[UserContent]) -> str | None:
        """Return the command of a text prompt starting with the command prefix, or None."""
        if self.command_prefix is None or len(prompt) != 1 or not isinstance(prompt[0], str):
            return None
        return strip_command_prefix(prompt[0], self.command_prefix)

    async def run_command(self, tape: Tape, line: str) -> str:
        """Run a command on ``tape`` and return its rendered output; record it as a ``command`` event.

        ``name args...`` calls the tool ``name`` with positional and then ``key=value``
        arguments, positional ones taking the tool's parameters in order. Any other line runs
        through the ``bash`` tool when the agent has one. Commands call tools without model
        calls, emitting tool events; the outcome is reported as a ``CommandFinished`` event.
        """
        if not line:
            raise ValueError("empty command")
        name, tokens = parse_command(line)
        start = time.monotonic()
        context = ToolContext(
            tape=tape, run_id="run_command", state=tape.context.state, command=True, events=self.events
        )
        output = ""
        status = "ok"
        try:
            if name not in self.tools:
                if "bash" not in self.tools:
                    raise ValueError("bash tool is not available")  # noqa: TRY301
                command_tool = self.tools["bash"]
                arguments = {"command": line}
            else:
                command_tool = self.tools[name]
                arguments = parse_command_args(tokens).bind(list(command_tool.parameters.get("properties", {})))
            result = await ToolExecutor(self.events, render=False).execute(command_tool, arguments, context=context)
            output = command_tool.render(result)
        except Exception as exc:
            status = "error"
            output = f"{exc!s}"
            raise
        else:
            return output
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            await self._emit(
                CommandFinished, tape, line=line, name=name, status=status, elapsed_ms=elapsed_ms, output=output
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
        self,
        tape: Tape,
        client: ModelClient,
        options: dict[str, Any],
        prompt: list[UserContent],
        state: StreamState,
        tracker: _RunTracker,
    ) -> AsyncGenerator[StreamEvent, None]:
        auto_handoff_remaining = MAX_AUTO_HANDOFF_RETRIES
        next_prompt: list[UserContent] | None = prompt
        try:
            for step in range(1, self.max_steps + 1):
                tracker.steps = step
                start = time.monotonic()
                should_continue = False
                logger.info("loop.step step={} tape={} model={}", step, tape.name, client.spec)
                await self._emit(StepStarted, tape, step=step, prompt=next_prompt)
                try:
                    output = self.run_once(next_prompt, model=client, options=options, tape=tape, state=state)
                    async with aclosing(output):
                        async for event in output:
                            tracker.observe(event)
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

                if not (should_continue or self.has_pending_messages(tape)):
                    await self._record_step(tape, step, start, "ok")
                    tracker.status = "ok"
                    return

                next_prompt = await self.continue_prompt(tape, next_prompt, state)
                await self._record_step(tape, step, start, "continue")

            raise RuntimeError(f"max_steps_reached={self.max_steps}")  # noqa: TRY301
        except Exception as exc:
            tracker.status, tracker.error = "error", f"{exc!s}"
            raise
        finally:
            tracker.end_trace()

    async def _record_step(self, tape: Tape, step: int, start: float, status: str, *, error: str | None = None) -> None:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await self._emit(StepFinished, tape, step=step, status=status, elapsed_ms=elapsed_ms, error=error)

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
                ChatFinished,
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
        request: LLMCallRequest,
        tools: list[Tool],
        state: StreamState,
    ) -> AsyncGenerator[tuple[str, republic.Stream[Any], AsyncIterator[StreamEvent]]]:
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
                "tools": [
                    *(republic.Tool(item.name, item.description, item.parameters) for item in tools),
                    *self.builtin_tools,
                ],
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
                await self._fire_llm_call_finished(tape, request, "".join(text), calls, state, started, error=exc)
            raise
        else:
            completed = stream.response
            await self._fire_llm_call_finished(
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

    async def _fire_llm_call_finished(
        self,
        tape: Tape,
        request: LLMCallRequest,
        text: str,
        tool_calls: list[republic.ToolCall],
        state: StreamState,
        started: datetime,
        error: Exception | None = None,
    ) -> None:
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        result = LLMCallResult(
            run_id=request.run_id,
            text=text or None,
            tool_calls=[asdict(call) for call in tool_calls],
            usage=state.usage,
            error=error,
            duration_ms=duration_ms,
        )
        await self._emit(LLMCallFinished, tape, request=request, result=result)


def events_from(items: Iterable[StreamEvent]) -> AsyncStreamEvents:
    """Stream fixed events."""

    async def generator() -> AsyncIterator[StreamEvent]:
        for item in items:
            yield item

    return AsyncStreamEvents(generator())


def _fixed_stream(
    text: str, *, ok: bool, span: Span | None, on_close: Callable[[], Awaitable[None]] | None
) -> AsyncStreamEvents:
    """Stream a run that answers ``text`` without calling the model, such as a command."""
    if span is not None:
        span.messages("gen_ai.output.messages", [{"role": "assistant", "content": text}])

    async def generator() -> AsyncIterator[StreamEvent]:
        yield StreamEvent("text", {"delta": text})
        yield StreamEvent("final", {"text": text, "ok": ok})

    return AsyncStreamEvents(generator(), on_close=on_close, span=span)


class _RunTracker:
    """How a run ended, for ``RunFinished``, and its output messages, for its ``invoke_agent`` span."""

    def __init__(self, span: Span | None) -> None:
        self.status = "cancelled"
        self.steps = 0
        self.error: str | None = None
        self._span = span
        self._messages: list[dict[str, Any]] = []
        self._text: list[str] = []
        self._calls: list[dict[str, Any]] = []

    def observe(self, event: StreamEvent) -> None:
        if self._span is None or not self._span.recording:
            return
        if event.kind == "text":
            self._text.append(str(event.data.get("delta", "")))
        elif event.kind == "tool_call":
            self._calls = event.data.get("tool_calls", [])
            self._messages.append({"role": "assistant", "content": "".join(self._text), "tool_calls": self._calls})
            self._text.clear()
        elif event.kind == "tool_result":
            for call, result in zip(self._calls, event.data.get("tool_results", []), strict=False):
                self._messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        elif event.kind == "error":
            self._span.fail(RuntimeError(str(event.data.get("message", "agent failed"))))

    def end_trace(self) -> None:
        if self._span is None:
            return
        if self._text:
            self._messages.append({"role": "assistant", "content": "".join(self._text)})
        self._span.messages("gen_ai.output.messages", self._messages)


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
    ``ToolCallStarted`` handlers can recover it into a guidance
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
