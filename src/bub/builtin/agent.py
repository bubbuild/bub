"""Runtime engine to process prompts with any-llm-sdk."""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection, Coroutine, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
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

from bub.builtin.settings import ModelCandidate, load_settings
from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import TapeService
from bub.framework import BubFramework
from bub.runtime import AsyncStreamEvents, BubError, ErrorKind, StreamEvent, StreamState
from bub.skills import discover_skills, render_skills_prompt
from bub.tape import InMemoryTapeStore, Tape
from bub.tools import (
    REGISTRY,
    Tool,
    ToolContext,
    ToolExecutor,
)
from bub.types import State
from bub.utils import workspace_from_state

CONTINUE_PROMPT = "Continue the task until all targets are completed."
HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
_CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)
MAX_AUTO_HANDOFF_RETRIES = 1
TOOL_ARGUMENTS_ADAPTER = TypeAdapter(dict[str, Any])


class Agent:
    """Agent that processes prompts using hooks, tools, tape, and any-llm-sdk."""

    def __init__(self, framework: BubFramework) -> None:
        self.settings = load_settings()
        self.framework = framework

    @cached_property
    def tapes(self) -> TapeService:
        import bub

        tape_store = self.framework.get_tape_store()
        if tape_store is None:
            tape_store = InMemoryTapeStore()
        tape_store = ForkTapeStore(tape_store)
        return TapeService(bub.home / "tapes", tape_store, self.framework.build_tape_context())

    @staticmethod
    def _events_from_iterable(iterable: Iterable) -> AsyncStreamEvents:
        async def generator() -> AsyncIterator:
            for item in iterable:
                yield item

        return AsyncStreamEvents(generator())

    @staticmethod
    def _events_with_callback(
        events: AsyncStreamEvents, callback: Callable[[], Coroutine[Any, Any, Any]]
    ) -> AsyncStreamEvents:
        async def generator() -> AsyncIterator[StreamEvent]:
            try:
                async for event in events:
                    yield event
            finally:
                await callback()

        return AsyncStreamEvents(generator(), state=events._state)

    async def run(
        self,
        *,
        session_id: str,
        prompt: str | list[dict],
        state: State,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> str:
        output: list[str] = []
        stream = await self.run_stream(
            session_id=session_id,
            prompt=prompt,
            state=state,
            model=model,
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
        )
        async for event in stream:
            if event.kind == "text":
                output.append(str(event.data.get("delta", "")))
        return "".join(output)

    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str | list[dict],
        state: State,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> AsyncStreamEvents:
        if not prompt:
            return self._events_from_iterable([
                StreamEvent("text", {"delta": "error: empty prompt"}),
                StreamEvent("final", {"text": "error: empty prompt", "ok": False}),
            ])

        tape = self.tapes.session_tape(session_id, workspace_from_state(state))
        tape.context = replace(tape.context, state=state)
        merge_back = not session_id.startswith("temp/")
        stack = AsyncExitStack()
        # The fork_tape context manager must not be exited until the last chunk of the stream is consumed.
        await stack.enter_async_context(self.tapes.fork_tape(tape.name, merge_back=merge_back))
        await self.tapes.ensure_bootstrap_anchor(tape.name)
        if isinstance(prompt, str) and prompt.strip().startswith(","):
            result = await self._run_command(tape=tape, line=prompt.strip())
            events = self._events_from_iterable([
                StreamEvent("text", {"delta": result}),
                StreamEvent("final", {"text": result, "ok": True}),
            ])
        else:
            events = await self._agent_loop(
                tape=tape,
                prompt=prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
            )
        return self._events_with_callback(events, callback=stack.aclose)

    async def _run_command(self, tape: Tape, *, line: str) -> str:
        line = line[1:].strip()
        if not line:
            raise ValueError("empty command")

        name, arg_tokens = _parse_internal_command(line)
        start = time.monotonic()
        context = ToolContext(tape=tape.name, run_id="run_command", state=tape.context.state)
        output = ""
        status = "ok"
        try:
            if name not in REGISTRY:
                output = await REGISTRY["bash"].run(context=context, cmd=line)
            else:
                args = _parse_args(arg_tokens)
                if REGISTRY[name].context:
                    args.kwargs["context"] = context
                output = REGISTRY[name].run(*args.positional, **args.kwargs)
                if inspect.isawaitable(output):
                    output = await output
        except Exception as exc:
            status = "error"
            output = f"{exc!s}"
            raise
        else:
            return output if isinstance(output, str) else str(output)
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            output_text = output if isinstance(output, str) else str(output)

            event_payload = {
                "raw": line,
                "name": name,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "output": output_text,
                "date": datetime.now(UTC).isoformat(),
            }
            await self.tapes.append_event(tape.name, "command", event_payload)

    async def _agent_loop(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> AsyncStreamEvents:
        next_prompt: str | list[dict] = prompt
        display_model = model or self.settings.model
        await self.tapes.append_event(
            tape.name,
            "loop.start",
            {
                "model": display_model,
                "prompt": prompt,
                "allowed_skills": list(allowed_skills) if allowed_skills else None,
                "allowed_tools": list(allowed_tools) if allowed_tools else None,
            },
        )
        state = StreamState()
        iterator = self._stream_events_with_auto_handoff(
            tape=tape,
            prompt=next_prompt,
            state=state,
            model=model,
            allowed_skills=allowed_skills,
            allowed_tools=allowed_tools,
        )
        return AsyncStreamEvents(iterator, state=state)

    async def _stream_events_with_auto_handoff(
        self,
        tape: Tape,
        prompt: str | list[dict],
        state: StreamState,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        auto_handoff_remaining = MAX_AUTO_HANDOFF_RETRIES
        display_model = model or self.settings.model
        next_prompt = prompt
        for step in range(1, self.settings.max_steps + 1):
            start = time.monotonic()
            should_continue = False
            logger.info("loop.step step={} tape={} model={}", step, tape.name, display_model)
            await self.tapes.append_event(tape.name, "loop.step.start", {"step": step, "prompt": next_prompt})
            try:
                output = await self._run_once(
                    tape=tape,
                    prompt=next_prompt,
                    model=model,
                    allowed_skills=allowed_skills,
                    allowed_tools=allowed_tools,
                )
                async for event in output:
                    yield event
                    if event.kind == "error":
                        elapsed_ms = int((time.monotonic() - start) * 1000)
                        await self.tapes.append_event(
                            tape.name,
                            "loop.step",
                            {
                                "step": step,
                                "elapsed_ms": elapsed_ms,
                                "status": "error",
                                "error": event.data.get("message", ""),
                                "date": datetime.now(UTC).isoformat(),
                            },
                        )
                    elif event.kind == "final":
                        should_continue = bool(event.data.get("tool_calls") or event.data.get("tool_results"))
            except Exception as exc:
                error_message = f"{exc!s}"
                elapsed_ms = int((time.monotonic() - start) * 1000)
                if auto_handoff_remaining > 0 and _is_context_length_error(error_message):
                    auto_handoff_remaining -= 1
                    logger.warning(
                        "auto_handoff: context length exceeded, performing automatic handoff. tape={} step={}",
                        tape.name,
                        step,
                    )
                    await self.tapes.handoff(
                        tape.name,
                        name="auto_handoff/context_overflow",
                        state={"reason": "context_length_exceeded", "error": error_message},
                    )
                    await self.tapes.append_event(
                        tape.name,
                        "loop.step",
                        {
                            "step": step,
                            "elapsed_ms": elapsed_ms,
                            "status": "auto_handoff",
                            "error": error_message,
                            "date": datetime.now(UTC).isoformat(),
                        },
                    )
                    next_prompt = prompt
                    continue

                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "error",
                        "error": error_message,
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                raise

            state.error = output.error
            state.usage = output.usage
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if not should_continue:
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "ok",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                return

            next_prompt = self._continue_prompt(tape)
            await self.tapes.append_event(
                tape.name,
                "loop.step",
                {
                    "step": step,
                    "elapsed_ms": elapsed_ms,
                    "status": "continue",
                    "date": datetime.now(UTC).isoformat(),
                },
            )

        raise RuntimeError(f"max_steps_reached={self.settings.max_steps}")

    def _load_skills_prompt(self, prompt: str, workspace: Path, allowed_skills: set[str] | None = None) -> str:
        skill_index = {
            skill.name.casefold(): skill
            for skill in discover_skills(workspace)
            if allowed_skills is None or skill.name.casefold() in allowed_skills
        }
        expanded_skills = set(HINT_RE.findall(prompt)) & set(skill_index.keys())
        return render_skills_prompt(list(skill_index.values()), expanded_skills=expanded_skills)

    async def _run_once(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = None,
        allowed_tools: Collection[str] | None = None,
        allowed_skills: Collection[str] | None = None,
    ) -> AsyncStreamEvents:
        prompt_text = prompt if isinstance(prompt, str) else _extract_text_from_parts(prompt)
        if allowed_tools is not None:
            from bub.builtin.tools import resolve_tool_names

            allowed_tools = resolve_tool_names(allowed_tools)
        if allowed_skills is not None:
            allowed_skills = {name.casefold() for name in allowed_skills}
            tape.context.state["allowed_skills"] = list(allowed_skills)
        if allowed_tools is not None:
            tools = [tool for tool in REGISTRY.values() if tool.name in allowed_tools]
        else:
            tools = list(REGISTRY.values())
        return await self._run_once_stream(
            tape=tape,
            prompt=prompt,
            prompt_text=prompt_text,
            model=model,
            allowed_skills=allowed_skills,
            tools=tools,
        )

    async def _run_once_stream(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        prompt_text: str,
        model: str | None,
        allowed_skills: set[str] | None,
        tools: list[Tool],
    ) -> AsyncStreamEvents:
        state = StreamState()

        async def iterator() -> AsyncGenerator[StreamEvent, None]:
            system_prompt = self._system_prompt(
                prompt_text, state=tape.context.state, allowed_skills=allowed_skills, tools=tools
            )
            prompt_message: dict[str, Any] = {"role": "user", "content": prompt}
            run_id = f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
            try:
                messages = await self.tapes.read_messages(tape)
            except BubError as exc:
                await self.tapes.record_chat(
                    tape=tape.name,
                    run_id=run_id,
                    system_prompt=system_prompt,
                    context_error=exc,
                    new_messages=[],
                    response_text=None,
                    error=exc,
                    model=model or self.settings.model,
                )
                raise
            if system_prompt:
                messages = [{"role": "system", "content": system_prompt}, *messages]
            messages.append(prompt_message)

            from bub.builtin.tools import model_tools

            model_tools_for_call = model_tools(tools)
            text_parts: list[str] = []
            tool_calls = _ToolCallAccumulator()
            response: ChatCompletion | ParsedChatCompletion[Any] | None = None
            async with asyncio.timeout(self.settings.model_timeout_seconds):
                completion = await self._completion_response(
                    model=model or self.settings.model,
                    messages=messages,
                    tools=model_tools_for_call,
                )
                if isinstance(completion, ChatCompletion):
                    response = completion
                async for event in _completion_events(completion, state, text_parts, tool_calls):
                    yield event

            assistant_message = response.choices[0].message if response is not None else None
            text = (
                assistant_message.content
                if assistant_message and assistant_message.content is not None
                else "".join(text_parts)
            )
            native_tool_calls = tool_calls.as_native()
            if native_tool_calls:
                tool_map = {tool_item.name: tool_item for tool_item in model_tools_for_call}
                serialized_tool_calls = [tool_call.model_dump(exclude_none=True) for tool_call in native_tool_calls]
                tool_invocations = [
                    _tool_invocation_from_native(tool_call, tool_map) for tool_call in native_tool_calls
                ]
                yield StreamEvent("tool_call", {"tool_calls": serialized_tool_calls})
                context = ToolContext(tape=tape.name, run_id=run_id, state=tape.context.state)
                execution = await ToolExecutor().execute_async(
                    tool_invocations,
                    context=context,
                )
                await self.tapes.record_chat(
                    tape=tape.name,
                    run_id=run_id,
                    system_prompt=system_prompt,
                    new_messages=[prompt_message],
                    response_text=None,
                    tool_calls=serialized_tool_calls,
                    tool_results=execution.tool_results,
                    response=response,
                    model=model or self.settings.model,
                    usage=state.usage,
                )
                yield StreamEvent("tool_result", {"tool_results": execution.tool_results})
                yield StreamEvent(
                    "final", {"ok": True, "tool_calls": serialized_tool_calls, "tool_results": execution.tool_results}
                )
                return

            await self.tapes.record_chat(
                tape=tape.name,
                run_id=run_id,
                system_prompt=system_prompt,
                new_messages=[prompt_message],
                response_text=text,
                response=response,
                model=model or self.settings.model,
                usage=state.usage,
            )
            yield StreamEvent("final", {"ok": True, "text": text})

        return AsyncStreamEvents(iterator(), state=state)

    def _build_llm(self, candidate: ModelCandidate) -> AnyLLM:
        return AnyLLM.create(
            candidate.provider,
            **self.settings.model_client_kwargs(candidate.provider),
        )

    async def _completion_response(
        self, *, model: str, messages: list[dict[str, Any]], tools: list[Tool]
    ) -> ChatCompletion | ParsedChatCompletion[Any] | AsyncIterator[ChatCompletionChunk]:
        from bub.builtin.tools import completion_tools

        tool_payloads = completion_tools(tools) or None
        completion_messages: list[dict[str, Any] | ChatCompletionMessage] = list(messages)
        candidates = self.settings.model_candidates(model)
        for index, candidate in enumerate(candidates):
            try:
                llm = self._build_llm(candidate)
                return await llm.acompletion(
                    model=candidate.model_id,
                    messages=completion_messages,
                    tools=tool_payloads,
                    max_tokens=self.settings.max_tokens,
                    stream=llm.SUPPORTS_COMPLETION_STREAMING,
                )
            except Exception as exc:
                if index == len(candidates) - 1:
                    raise
                logger.warning("model candidate failed; trying fallback model={} error={}", candidate.name, exc)

        raise RuntimeError("no model candidates available")

    def _system_prompt(
        self, prompt: str, state: State, allowed_skills: set[str] | None = None, tools: Iterable[Tool] | None = None
    ) -> str:
        from bub.builtin.tools import render_tools_prompt

        blocks: list[str] = []
        if result := self.framework.get_system_prompt(prompt=prompt, state=state):
            blocks.append(result)
        tools_prompt = render_tools_prompt(tools if tools is not None else REGISTRY.values())
        if tools_prompt:
            blocks.append(tools_prompt)
        workspace = workspace_from_state(state)
        if skills_prompt := self._load_skills_prompt(prompt, workspace, allowed_skills):
            blocks.append(skills_prompt)
        return "\n\n".join(blocks)

    def _continue_prompt(self, tape: Tape) -> str:
        if "context" in tape.context.state:
            return f"{CONTINUE_PROMPT} [context: {tape.context.state['context']}]"
        return CONTINUE_PROMPT


@dataclass
class _StreamToolCall:
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


class _ToolCallAccumulator:
    def __init__(self) -> None:
        self._message_calls: list[ChatCompletionMessageToolCall] = []
        self._stream_calls: dict[int, _StreamToolCall] = {}

    def add_message_calls(self, calls: Iterable[ChatCompletionMessageToolCall]) -> None:
        self._message_calls.extend(calls)

    def merge_delta_calls(self, deltas: Iterable[ChoiceDeltaToolCall]) -> None:
        for delta in deltas:
            self._stream_calls.setdefault(delta.index, _StreamToolCall()).merge(delta)

    def as_native(self) -> list[ChatCompletionMessageToolCall]:
        if self._message_calls:
            return list(self._message_calls)
        return [self._stream_calls[index].as_tool_call(index) for index in sorted(self._stream_calls)]


def _tool_invocation_from_native(
    tool_call: ChatCompletionMessageToolCall,
    tool_map: dict[str, Tool],
) -> tuple[Tool, dict[str, Any]]:
    tool_name, arguments = _parse_native_function_call(tool_call)
    tool_obj = tool_map.get(tool_name)
    if tool_obj is None:
        raise BubError(ErrorKind.TOOL, f"Unknown tool name: {tool_name}.")
    return tool_obj, arguments


def _parse_native_function_call(tool_call: ChatCompletionMessageToolCall) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, ChatCompletionMessageFunctionToolCall):
        raise BubError(ErrorKind.INVALID_INPUT, "Expected a function tool call with JSON object arguments.")
    try:
        arguments = TOOL_ARGUMENTS_ADAPTER.validate_json(tool_call.function.arguments or "{}")
    except ValidationError as exc:
        raise BubError(ErrorKind.INVALID_INPUT, "Expected a function tool call with JSON object arguments.") from exc
    return tool_call.function.name, arguments


async def _completion_events(
    completion: ChatCompletion | ParsedChatCompletion[Any] | AsyncIterator[ChatCompletionChunk],
    state: StreamState,
    text_parts: list[str],
    tool_calls: _ToolCallAccumulator,
) -> AsyncGenerator[StreamEvent, None]:
    if isinstance(completion, ChatCompletion):
        if usage := TapeService._extract_usage(completion):
            state.usage = usage
        message = completion.choices[0].message
        for event in _completion_message_events(message, text_parts, tool_calls):
            yield event
        return

    async for chunk in completion:
        async for event in _completion_chunk_events(chunk, state, text_parts, tool_calls):
            yield event


def _completion_message_events(
    message: ChatCompletionMessage,
    text_parts: list[str],
    tool_calls: _ToolCallAccumulator,
) -> Iterable[StreamEvent]:
    if message.reasoning:
        yield StreamEvent("reasoning", {"delta": _reasoning_text(message.reasoning)})
    if message.content:
        text_parts.append(message.content)
        yield StreamEvent("text", {"delta": message.content})
    tool_calls.add_message_calls(cast("Iterable[ChatCompletionMessageToolCall]", message.tool_calls or []))


async def _completion_chunk_events(
    chunk: ChatCompletionChunk,
    state: StreamState,
    text_parts: list[str],
    tool_calls: _ToolCallAccumulator,
) -> AsyncGenerator[StreamEvent, None]:
    if usage := TapeService._extract_usage(chunk):
        state.usage = usage
    for choice in chunk.choices:
        delta = choice.delta
        if delta.reasoning:
            yield StreamEvent("reasoning", {"delta": _reasoning_text(delta.reasoning)})
        if delta.content:
            text_parts.append(delta.content)
            yield StreamEvent("text", {"delta": delta.content})
        if delta.tool_calls:
            tool_calls.merge_delta_calls(delta.tool_calls)


def _reasoning_text(reasoning: object) -> str:
    content = getattr(reasoning, "content", reasoning)
    return "" if content is None else str(content)


@dataclass(frozen=True)
class Args:
    positional: list[str]
    kwargs: dict[str, Any]


def _parse_internal_command(line: str) -> tuple[str, list[str]]:
    body = line.strip()
    words = shlex.split(body)
    if not words:
        return "", []
    return words[0], words[1:]


def _parse_args(args_tokens: list[str]) -> Args:
    positional: list[str] = []
    kwargs: dict[str, str] = {}
    first_kwarg = False
    for token in args_tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            kwargs[key] = value
            first_kwarg = True
        elif first_kwarg:
            raise ValueError(f"positional argument '{token}' cannot appear after keyword arguments")
        else:
            positional.append(token)
    return Args(positional=positional, kwargs=kwargs)


def _is_context_length_error(error_msg: str) -> bool:
    """Check whether an error message indicates a context-length / prompt-too-long failure."""
    return bool(_CONTEXT_LENGTH_PATTERNS.search(error_msg))


def _extract_text_from_parts(parts: list[dict]) -> str:
    """Extract text content from multimodal content parts."""
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
