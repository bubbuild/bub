"""Runtime engine to process prompts with any-llm-sdk."""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
import time
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Iterable
from contextlib import AsyncExitStack, aclosing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from loguru import logger

from bub.builtin.model_runner import (
    ModelRunner,
    is_context_length_error,
)
from bub.builtin.settings import load_settings
from bub.envelope import field_of
from bub.framework import BubFramework
from bub.skills import discover_skills, render_skills_prompt
from bub.store import AsyncTapeStore, AsyncTapeStoreAdapter, InMemoryTapeStore, TapeStore, is_async_tape_store
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tape import Tape
from bub.tools import REGISTRY, Tool, ToolContext, model_tools
from bub.tracing import Span, current_span
from bub.turn import TurnState
from bub.utils import workspace_from_state

HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
MAX_AUTO_HANDOFF_RETRIES = 1


class Agent:
    """Agent that processes prompts using hooks, tools, tape, and any-llm-sdk."""

    def __init__(
        self,
        framework: BubFramework,
        *,
        tools: Collection[Tool] | None = None,
        tape_store: TapeStore | AsyncTapeStore | None = None,
        skill_dirs: Collection[Path] | None = None,
    ) -> None:
        """Create a builtin agent with instance-specific tools, skills, and storage.

        Args:
            framework: Configured hook runtime supplying prompts, tape context,
                interception hooks, and optional shared resources.
            tools: Tools available to this instance. None snapshots the global
                registry; an empty collection disables tools.
            tape_store: Explicit store, preferred over the framework's active
                store. Without either, the agent uses an in-memory store.
            skill_dirs: Skill roots in precedence order. None uses project, user,
                and builtin discovery; an empty collection disables discovery.

        Settings come from Bub's process-wide configuration. The caller owns the
        lifecycle of an explicitly supplied store.
        """
        self.settings = load_settings()
        self.framework = framework
        self.tools = {tool.name: tool for tool in tools} if tools is not None else REGISTRY.copy()
        self.tape_store = tape_store
        self.skill_dirs = skill_dirs
        self.model_runner = ModelRunner(self.settings, hooks=framework.get_agent_hooks())

    @cached_property
    def tape(self) -> Tape:
        """Return the lazily constructed, cached tape factory for this agent.

        Select the explicit store, active framework store, or an in-memory fallback,
        in that order. Adapt synchronous stores and use hook-provided context and
        sidecars. Archive files use ``bub.home / 'tapes'`` independently of the store.
        """
        import bub

        tape_store: TapeStore | AsyncTapeStore | None
        if self.tape_store is not None:
            tape_store = self.tape_store
        else:
            tape_store = self.framework.get_tape_store()
            if tape_store is None:
                tape_store = InMemoryTapeStore()
        if not is_async_tape_store(tape_store):
            tape_store = AsyncTapeStoreAdapter(tape_store)
        return Tape(
            bub.home / "tapes",
            tape_store,
            self.framework.build_tape_context(),
            sidecars=self.framework.get_tape_sidecars(),
        )

    @staticmethod
    def _events_from_iterable(iterable: Iterable) -> AsyncStreamEvents:
        async def generator() -> AsyncIterator:
            for item in iterable:
                yield item

        return AsyncStreamEvents(generator())

    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str | list[dict],
        state: TurnState | None = None,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncStreamEvents:
        """Prepare a turn and return its stream; await this method before iterating.

        Args:
            session_id: Session identity within the workspace. A ``temp/`` prefix
                prevents the turn's fork from merging back into its parent tape.
            prompt: Text or multimodal content parts. Text beginning with a comma
                after stripping whitespace invokes a builtin command.
            state: Mutable turn state. None loads state through framework hooks
                using this agent's store; supplied state skips that loading.
                The current agent is always bound into the state.
            model: Per-turn override, ahead of the state and configured model.
            allowed_skills: Case-insensitive skill names available to this turn;
                None leaves discovery unrestricted.
            allowed_tools: Instance tool names or model aliases for the agent loop;
                None allows all instance tools and an empty collection allows none.
                Command execution uses the instance's tools directly.
            reasoning_effort: Per-turn override of the value in state.

        Consume the stream to completion to finish execution and tape merging.
        A ``final`` event ends a model step, not necessarily the whole turn.
        The returned object exposes ``error`` and ``usage``; execution can also
        raise exceptions. This method does not render or dispatch outbound messages,
        call save-state hooks, or serialize concurrent turns in the same session.
        """
        span = Span(
            "invoke_agent bub",
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "bub",
                "gen_ai.conversation.id": session_id,
            },
        )
        span.messages("gen_ai.input.messages", [{"role": "user", "content": prompt}])
        stack = AsyncExitStack()
        try:
            with span.activate():
                if not prompt:
                    events = self._events_from_iterable([
                        StreamEvent("text", {"delta": "error: empty prompt"}),
                        StreamEvent("final", {"text": "error: empty prompt", "ok": False}),
                    ])
                else:
                    if state is None:
                        state = await self.framework.build_state({"_runtime_agent": self}, session_id)
                    state["_runtime_agent"] = self
                    if model is None:
                        model = state.get("model")
                    if reasoning_effort is not None:
                        state["reasoning_effort"] = reasoning_effort
                    state.setdefault("session_id", session_id)
                    tape = self.tape.session_tape(
                        session_id, workspace_from_state(state), context=replace(self.tape.context, state=state)
                    )
                    # Keep the tape fork open until the stream closes, even if it is never consumed.
                    tape = await stack.enter_async_context(
                        tape.fork_tape(merge_back=not session_id.startswith("temp/"))
                    )
                    await tape.ensure_bootstrap_anchor()
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
        except BaseException as exc:
            span.fail(exc)
            try:
                with span.activate():
                    await stack.aclose()
            finally:
                span.end()
            raise
        return AsyncStreamEvents(
            self._trace_events(events, span),
            state=events._state,
            on_close=stack.aclose,
            span=span,
        )

    @staticmethod
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

    async def _run_command(self, tape: Tape, *, line: str) -> str:
        line = line[1:].strip()
        if not line:
            raise ValueError("empty command")

        name, arg_tokens = _parse_internal_command(line)
        start = time.monotonic()
        context = ToolContext(tape=tape, run_id="run_command", state=tape.context.state)
        output = ""
        status = "ok"
        try:
            if name not in self.tools:
                if "bash" not in self.tools:
                    raise ValueError("bash tool is not available")  # noqa: TRY301
                output = await self.tools["bash"].run(context=context, command=line)
            else:
                args = _parse_args(arg_tokens)
                if self.tools[name].context:
                    args.kwargs["context"] = context
                output = self.tools[name].run(*args.positional, **args.kwargs)
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
            await tape.append_event("command", event_payload)

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
        await tape.append_event(
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
            await tape.append_event("loop.step.start", {"step": step, "prompt": next_prompt})
            try:
                output = await self._run_once(
                    tape=tape,
                    prompt=next_prompt,
                    model=model,
                    allowed_skills=allowed_skills,
                    allowed_tools=allowed_tools,
                )
                async with aclosing(output):
                    async for event in output:
                        yield event
                        if event.kind == "error":
                            elapsed_ms = int((time.monotonic() - start) * 1000)
                            await tape.append_event(
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
                    await tape.append_event(
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

                await tape.append_event(
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
            should_continue = should_continue or self._has_steering_messages(tape.context.state)
            if not should_continue:
                await tape.append_event(
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "ok",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                return

            next_prompt = await self.framework.continue_prompt(prompt=next_prompt, tape=tape, state=state)
            await tape.append_event(
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
            for skill in discover_skills(workspace, skill_dirs=self.skill_dirs)
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

            allowed_tools = resolve_tool_names(allowed_tools, all_names=self.tools)
        if allowed_skills is not None:
            allowed_skills = {name.casefold() for name in allowed_skills}
            tape.context.state["allowed_skills"] = list(allowed_skills)
        if allowed_tools is not None:
            tools = [tool for tool in self.tools.values() if tool.name in allowed_tools]
        else:
            tools = list(self.tools.values())
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
        system_prompt = self._system_prompt(
            prompt_text, state=tape.context.state, allowed_skills=allowed_skills, tools=tools
        )
        resolved_model = model or self.settings.model

        model_tools_for_call = model_tools(tools)
        if (span := current_span()) and span.recording:
            span.set(**{
                "gen_ai.tool.definitions": [
                    tool.to_schema()["function"] | {"type": "function"} for tool in model_tools_for_call
                ]
            })
        steering_inbox = self.framework.get_steering_inbox()
        steering_envelopes = await steering_inbox.drain_messages(tape.context.state) if steering_inbox else []
        steering_messages = list(
            await asyncio.gather(*[
                self.framework.build_prompt(
                    message, session_id=field_of(message, "session_id"), state=tape.context.state
                )
                for message in steering_envelopes
            ])
        )
        return self.model_runner.run(
            tape=tape,
            model=resolved_model,
            tools=model_tools_for_call,
            system_prompt=system_prompt,
            prompt=prompt,
            steering_messages=steering_messages,
        )

    def _system_prompt(
        self,
        prompt: str,
        state: TurnState,
        allowed_skills: set[str] | None = None,
        tools: Iterable[Tool] | None = None,
    ) -> str:
        from bub.builtin.tools import render_tools_prompt

        blocks: list[str] = []
        if result := self.framework.get_system_prompt(prompt=prompt, state=state):
            blocks.append(result)
        tools_prompt = render_tools_prompt(tools if tools is not None else self.tools.values())
        if tools_prompt:
            blocks.append(tools_prompt)
        workspace = workspace_from_state(state)
        if skills_prompt := self._load_skills_prompt(prompt, workspace, allowed_skills):
            blocks.append(skills_prompt)
        return "\n\n".join(blocks)

    def _has_steering_messages(self, state: TurnState) -> bool:
        steering_inbox = self.framework.get_steering_inbox()
        return bool(steering_inbox and steering_inbox.message_count(state) > 0)


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


def _extract_text_from_parts(parts: list[dict]) -> str:
    """Extract text content from multimodal content parts."""
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
