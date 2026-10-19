"""Bub's agent: the core agent configured by settings and the framework's hooks."""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path

import bub.core as core
from bub.builtin.settings import load_settings
from bub.core.store import AsyncTapeStore, TapeStore
from bub.core.tape import Tape, session_tape_name
from bub.core.tools import Tool, model_tools
from bub.framework import BubFramework
from bub.hooks.agent import AgentPlugins
from bub.prompt import LegacyPrompt, UserContent, prompt_text, to_content
from bub.skills import discover_skills, render_skills_prompt
from bub.streaming import AsyncStreamEvents
from bub.turn import TurnState
from bub.utils import workspace_from_state

HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
ALLOWED_SKILLS_STATE_KEY = "allowed_skills"


class BubAgent(core.Agent):
    """Bub's agent: the core loop configured by settings and the framework's hooks.

    Adds commands, skills, code mode and deferred tools to :class:`bub.core.Agent`, and
    connects the framework's agent-loop hooks through :class:`AgentPlugins`. System prompt
    sections, in order: code mode, deferred tools, skills, the framework's ``system_prompt``
    hooks, then sections added with ``system_prompt.add``. The framework's hooks handle
    events before handlers added with ``events.on``.

    One instance serves any number of sessions: per-turn choices (model, allowed tools
    and skills) live in the turn state.
    """

    command_prefix: str

    def __init__(
        self,
        framework: BubFramework,
        *,
        tools: Collection[Tool] | None = None,
        tape_store: TapeStore | AsyncTapeStore | None = None,
        skill_dirs: Collection[Path] | None = None,
        command_prefix: str | None = None,
    ) -> None:
        """Create a Bub agent with instance-specific tools, skills, and storage.

        Args:
            framework: Configured hook runtime supplying prompts, tape context,
                interception hooks, and optional shared resources.
            tools: Tools available to this instance. None uses the framework's
                ``provide_tools`` tools; an empty collection disables tools.
            tape_store: Explicit store, preferred over the framework's active store.
                Create the agent inside ``framework.running()`` to use the latter.
                Without either, the agent uses an in-memory store.
            skill_dirs: Skill roots in precedence order. None uses project, user,
                and builtin discovery; an empty collection disables discovery.
            command_prefix: Non-empty prefix without whitespace. None uses the
                configured prefix (a comma by default).

        Settings come from Bub's process-wide configuration. The caller owns the
        lifecycle of an explicitly supplied store.
        """
        self.settings = load_settings()
        self.framework = framework
        self.skill_dirs = skill_dirs
        self.plugins = AgentPlugins(framework)
        super().__init__(
            store=framework.get_tape_store() if tape_store is None else tape_store,
            tools=() if tools is None else tools,
            system_prompt=[
                self._code_mode_prompt,
                self._deferred_tools_prompt,
                self._skills_prompt,
            ],
            context=framework.build_tape_context(),
            sidecars=framework.get_tape_sidecars(),
            max_steps=self.settings.max_steps,
            name="bub",
            command_prefix=self.settings.command_prefix if command_prefix is None else command_prefix,
        )
        self.plugins.attach(self, tools=tools is None)

    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: list[UserContent] | LegacyPrompt,
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
            prompt: User content (text, images, audio and video); legacy text and content blocks are converted.
                A text-only prompt beginning with this agent's command prefix after
                stripping whitespace invokes a command.
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

        Loads the turn state and starts the turn with :meth:`start` on the session's tape.
        Consume the stream to completion to finish execution and tape merging. A
        ``final`` event ends a model step, not necessarily the whole turn. The returned
        object exposes ``error`` and ``usage``; execution can also raise exceptions. This
        method does not render or dispatch outbound messages, call save-state hooks, or
        serialize concurrent turns in the same session.
        """
        content = to_content(prompt)
        if state is None:
            state = await self.framework.build_state({"_runtime_agent": self}, session_id)
        state["_runtime_agent"] = self
        if reasoning_effort is not None:
            state["reasoning_effort"] = reasoning_effort
        state.setdefault("session_id", session_id)
        metadata = None
        if self.command_line(content) is None:
            self._scope_turn(state, allowed_skills=allowed_skills, allowed_tools=allowed_tools)
            metadata = {
                "allowed_skills": list(allowed_skills) if allowed_skills else None,
                "allowed_tools": list(allowed_tools) if allowed_tools else None,
            }
        return await self.start(
            content,
            # The turn's model and reasoning effort apply until the next turn.
            model=self.settings.chat_model(
                state.get("model") if model is None else model, reasoning_effort=state.get("reasoning_effort")
            ),
            tape=session_tape_name(session_id, workspace_from_state(state)),
            state=state,
            merge_back=not session_id.startswith("temp/"),
            metadata=metadata,
        )

    def _scope_turn(
        self, state: TurnState, *, allowed_skills: Collection[str] | None, allowed_tools: Collection[str] | None
    ) -> None:
        """Record the turn's allowed skills and tools in state; unknown tool names raise ``ValueError``."""
        from bub.builtin.tools import ALLOWED_TOOLS_STATE_KEY, resolve_tool_names

        if allowed_tools is None:
            state.pop(ALLOWED_TOOLS_STATE_KEY, None)
        else:
            state[ALLOWED_TOOLS_STATE_KEY] = sorted(resolve_tool_names(allowed_tools, all_names=self.tools))
        if allowed_skills is None:
            state.pop(ALLOWED_SKILLS_STATE_KEY, None)
        else:
            state[ALLOWED_SKILLS_STATE_KEY] = list({name.casefold() for name in allowed_skills})

    async def prepare_tools(self, tape: Tape, prompt: list[UserContent] | None) -> list[Tool]:
        from bub.builtin.codemode import CODE_TOOLS_STATE_KEY
        from bub.builtin.tools import TOOL_DESCRIBE_TOOL_NAME, loaded_tool_names, tool_describe

        state = tape.context.state
        # Code mode sees every allowed tool, deferred ones included; deferral applies to model-facing tools.
        tools, code_tools = self._split_code_mode(state)
        if code_tools is None:
            state.pop(CODE_TOOLS_STATE_KEY, None)
        else:
            state[CODE_TOOLS_STATE_KEY] = model_tools(code_tools)
        # ``tool.describe`` is exposed exactly when a deferred tool remains, since it is the only way to load one.
        direct = [tool for tool in tools if not tool.deferred and tool.name != TOOL_DESCRIBE_TOOL_NAME]
        deferred = {tool.name: tool for tool in _deferred_tools(tools)}
        if deferred:
            direct.append(self.tools.get(TOOL_DESCRIBE_TOOL_NAME, tool_describe))
            direct.extend(deferred[name] for name in await loaded_tool_names(tape) if name in deferred)
        return model_tools(direct)

    def _turn_tools(self, state: TurnState) -> list[Tool]:
        """Return the tools allowed in this turn, direct and deferred alike."""
        from bub.builtin.tools import ALLOWED_TOOLS_STATE_KEY

        if (allowed := state.get(ALLOWED_TOOLS_STATE_KEY)) is None:
            return list(self.tools.values())
        return [tool for name, tool in self.tools.items() if name in allowed]

    def _split_code_mode(self, state: TurnState) -> tuple[list[Tool], list[Tool] | None]:
        """Return the tools left to the model and, in code mode, the tools callable only from code.

        Code mode is a session setting (``state["code_mode"]``, switched by the ``code_mode``
        command) and applies only when ``run_code`` is among the allowed tools: the model then
        sees ``direct`` tools, and ``auto`` and ``code`` tools are callable only from code. Outside
        code mode, ``code`` tools are dropped.
        """
        from bub.builtin.codemode import CODE_MODE_STATE_KEY, RUN_CODE_TOOL_NAME

        tools = self._turn_tools(state)
        direct_tools = [tool for tool in tools if tool.name != RUN_CODE_TOOL_NAME]
        if not state.get(CODE_MODE_STATE_KEY) or len(direct_tools) == len(tools):
            return [tool for tool in direct_tools if tool.exposure != "code"], None
        code_tools = [tool for tool in direct_tools if tool.exposure in ("auto", "code")]
        return [tool for tool in tools if tool.exposure == "direct"], code_tools

    @staticmethod
    def _code_mode_prompt(context: core.PromptContext) -> str | None:
        from bub.builtin.codemode import CODE_MODE

        return CODE_MODE.prompt(context)

    def _deferred_tools_prompt(self, context: core.PromptContext) -> str:
        from bub.builtin.tools import render_deferred_tools_prompt

        tools, _ = self._split_code_mode(context.state)
        return render_deferred_tools_prompt(_deferred_tools(tools))

    def _skills_prompt(self, context: core.PromptContext) -> str:
        allowed = context.state.get(ALLOWED_SKILLS_STATE_KEY)
        return self._load_skills_prompt(
            prompt_text(context.prompt), workspace_from_state(context.state), None if allowed is None else set(allowed)
        )

    def _load_skills_prompt(self, prompt: str, workspace: Path, allowed_skills: set[str] | None = None) -> str:
        skill_index = {
            skill.name.casefold(): skill
            for skill in discover_skills(workspace, skill_dirs=self.skill_dirs)
            if allowed_skills is None or skill.name.casefold() in allowed_skills
        }
        expanded_skills = set(HINT_RE.findall(prompt)) & set(skill_index.keys())
        return render_skills_prompt(list(skill_index.values()), expanded_skills=expanded_skills)


def _deferred_tools(tools: list[Tool]) -> list[Tool]:
    return [tool for tool in tools if tool.deferred and tool.exposure in ("auto", "direct")]
