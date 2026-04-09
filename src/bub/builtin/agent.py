"""Republic-driven runtime engine to process prompts."""

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
from republic import (
    LLM,
    AsyncStreamEvents,
    AsyncTapeStore,
    StreamEvent,
    StreamState,
    TapeContext,
    ToolContext,
)
from republic.tape import InMemoryTapeStore, Tape

from bub.builtin.settings import AgentSettings, load_settings
from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import TapeService
from bub.framework import BubFramework
from bub.skills import discover_skills, render_skills_prompt
from bub.tools import REGISTRY, model_tools, render_tools_prompt
from bub.types import State
from bub.utils import workspace_from_state

CONTINUE_PROMPT = "Continue the task."
HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
_CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}length|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)
MAX_AUTO_HANDOFF_RETRIES = 1


class Agent:
    """Agent that processes prompts using hooks and tools. Backed by republic."""

    def __init__(self, framework: BubFramework) -> None:
        self.settings = load_settings()
        self.framework = framework

    @cached_property
    def tapes(self) -> TapeService:
        tape_store = self.framework.get_tape_store()
        if tape_store is None:
            tape_store = InMemoryTapeStore()
        tape_store = ForkTapeStore(tape_store)
        llm = _build_llm(self.settings, tape_store, self.framework.build_tape_context())
        return TapeService(llm, self.settings.home / "tapes", tape_store)

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
            async for event in events:
                yield event
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
    ) -> AsyncStreamEvents:
        if not prompt:
            events = [
                StreamEvent("text", {"delta": "error: empty prompt"}),
                StreamEvent("final", {"text": "error: empty prompt", "ok": False}),
            ]
            return self._events_from_iterable(events)

        tape = self.tapes.session_tape(session_id, workspace_from_state(state))
        tape.context = replace(tape.context, state=state)
        merge_back = not session_id.startswith("temp/")
        stack = AsyncExitStack()
        # the fork_tape context manager must not be exited until the last chunk of the stream is consumed.
        # So we use an AsyncExitStack and inject a callback to the iterator.
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
                tape=tape, prompt=prompt, model=model, allowed_skills=allowed_skills, allowed_tools=allowed_tools
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
            outcome = _ToolAutoOutcome(kind="text", text="", error="")
            logger.info("loop.step step={} tape={} model={}", step, tape.name, display_model)
            await self.tapes.append_event(tape.name, "loop.step.start", {"step": step, "prompt": next_prompt})
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
                    outcome = _resolve_tool_auto_result(event.data)

            state.error = output.error
            state.usage = output.usage
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if outcome.kind == "text":
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
            if outcome.kind == "continue":
                if "context" in tape.context.state:
                    next_prompt = f"{CONTINUE_PROMPT} [context: {tape.context.state['context']}]"
                else:
                    next_prompt = CONTINUE_PROMPT
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
                continue

            # Check if this is a context-length error that can be recovered via auto-handoff
            if auto_handoff_remaining > 0 and _is_context_length_error(outcome.error):
                auto_handoff_remaining -= 1
                logger.warning(
                    "auto_handoff: context length exceeded, performing automatic handoff. tape={} step={}",
                    tape.name,
                    step,
                )
                await self.tapes.handoff(
                    tape.name,
                    name="auto_handoff/context_overflow",
                    state={"reason": "context_length_exceeded", "error": outcome.error},
                )
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "auto_handoff",
                        "error": outcome.error,
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                # Retry with original prompt — the handoff anchor will truncate history
                next_prompt = prompt
                continue

            await self.tapes.append_event(
                tape.name,
                "loop.step",
                {
                    "step": step,
                    "elapsed_ms": elapsed_ms,
                    "status": "error",
                    "error": outcome.error,
                    "date": datetime.now(UTC).isoformat(),
                },
            )
            raise RuntimeError(outcome.error)

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
            allowed_tools = {name.casefold() for name in allowed_tools}
        if allowed_skills is not None:
            allowed_skills = {name.casefold() for name in allowed_skills}
            tape.context.state["allowed_skills"] = list(allowed_skills)
        if allowed_tools is not None:
            tools = [tool for tool in REGISTRY.values() if tool.name.casefold() in allowed_tools]
        else:
            tools = list(REGISTRY.values())
        async with asyncio.timeout(self.settings.model_timeout_seconds):
            return await tape.stream_events_async(
                prompt=prompt,
                system_prompt=self._system_prompt(prompt_text, state=tape.context.state, allowed_skills=allowed_skills),
                max_tokens=self.settings.max_tokens,
                tools=model_tools(tools),
                model=model,
            )

    def _system_prompt(self, prompt: str, state: State, allowed_skills: set[str] | None = None) -> str:
        blocks: list[str] = []
        if result := self.framework.get_system_prompt(prompt=prompt, state=state):
            blocks.append(result)
        tools_prompt = render_tools_prompt(REGISTRY.values())
        if tools_prompt:
            blocks.append(tools_prompt)
        workspace = workspace_from_state(state)
        if skills_prompt := self._load_skills_prompt(prompt, workspace, allowed_skills):
            blocks.append(skills_prompt)
        return "\n\n".join(blocks)


@dataclass(frozen=True)
class _ToolAutoOutcome:
    kind: str
    text: str = ""
    error: str = ""


def _resolve_tool_auto_result(final_data: dict[str, Any]) -> _ToolAutoOutcome:
    if (text := final_data.get("text")) is not None:
        return _ToolAutoOutcome(kind="text", text=text)
    if final_data.get("tool_calls") or final_data.get("tool_results"):
        return _ToolAutoOutcome(kind="continue")
    return _ToolAutoOutcome(kind="error", error="unknown error")


def _build_llm(settings: AgentSettings, tape_store: AsyncTapeStore, tape_context: TapeContext) -> LLM:
    from republic.auth.openai_codex import openai_codex_oauth_resolver

    return LLM(
        settings.model,
        api_key=settings.api_key,
        api_base=settings.api_base,
        fallback_models=settings.fallback_models,
        api_key_resolver=openai_codex_oauth_resolver(),
        tape_store=tape_store,
        client_args=settings.client_args,
        api_format=settings.api_format,
        context=tape_context,
        verbose=settings.verbose,
    )


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
