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
from typing import Any

from loguru import logger

from bub.builtin.model_runner import (
    ModelRunner,
    is_context_length_error,
)
from bub.builtin.settings import load_settings
from bub.builtin.tape import Tape
from bub.envelope import field_of
from bub.framework import BubFramework
from bub.runtime import AsyncStreamEvents, StreamEvent, StreamState
from bub.skills import discover_skills, render_skills_prompt
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, is_async_tape_store
from bub.tools import (
    REGISTRY,
    Tool,
    ToolContext,
)
from bub.types import State
from bub.utils import workspace_from_state

CONTINUE_PROMPT = "Continue the task until all targets are completed."
HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
MAX_AUTO_HANDOFF_RETRIES = 1


class Agent:
    """Agent that processes prompts using hooks, tools, tape, and any-llm-sdk."""

    def __init__(self, framework: BubFramework) -> None:
        self.settings = load_settings()
        self.framework = framework
        self.model_runner = ModelRunner(self.settings)

    @cached_property
    def tape(self) -> Tape:
        import bub

        tape_store = self.framework.get_tape_store()
        if tape_store is None:
            tape_store = InMemoryTapeStore()
        if not is_async_tape_store(tape_store):
            tape_store = AsyncTapeStoreAdapter(tape_store)
        return Tape(bub.home / "tapes", tape_store, self.framework.build_tape_context())

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

        state.setdefault("session_id", session_id)
        tape = self.tape.session_tape(
            session_id, workspace_from_state(state), context=replace(self.tape.context, state=state)
        )
        merge_back = not session_id.startswith("temp/")
        stack = AsyncExitStack()
        # The fork_tape context manager must not be exited until the last chunk of the stream is consumed.
        tape = await stack.enter_async_context(tape.fork_tape(merge_back=merge_back))
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

        return self._events_with_callback(events, callback=stack.aclose)

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

            next_prompt = self._continue_prompt(tape)
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
        from bub.builtin.tools import model_tools

        system_prompt = self._system_prompt(
            prompt_text, state=tape.context.state, allowed_skills=allowed_skills, tools=tools
        )
        resolved_model = model or self.settings.model

        model_tools_for_call = model_tools(tools)
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

    def _has_steering_messages(self, state: State) -> bool:
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
