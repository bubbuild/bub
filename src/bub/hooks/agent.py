"""Connect a framework's plugin hooks to a :class:`bub.core.Agent`."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, TypedDict

from bub.core.agent import Agent
from bub.core.events import MessagesPending, StepFinished
from bub.core.sidecars import TapeSidecar
from bub.core.store import AsyncTapeStore, TapeStore
from bub.core.system_prompt import PromptContext
from bub.core.tape import TapeContext
from bub.envelope import Envelope, field_of, normalize_envelope
from bub.turn import TurnState

if TYPE_CHECKING:
    from bub.framework import BubFramework


class AgentComponents(TypedDict, total=False):
    """Agent constructor arguments provided by plugin hooks."""

    store: TapeStore | AsyncTapeStore
    context: TapeContext
    sidecars: Sequence[TapeSidecar]


class AgentPlugins:
    """Use a framework's agent-loop hooks with any :class:`bub.core.Agent`.

    ::

        plugins = AgentPlugins(plugins=[MyPlugin()])
        async with plugins.running():
            agent = plugins.attach(Agent(model=model, system_prompt="...", **plugins.components()))
            state = await plugins.build_state("session-1", agent=agent)
            async with agent.run(prompt, tape="session-1", state=state) as stream: ...

    Covered hooks: ``provide_tape_store``, ``build_tape_context`` and ``provide_tape_sidecar``
    (constructor components), ``system_prompt`` and ``provide_tools`` (added by ``attach``), the
    ``before/after_llm_call`` and ``before/after_tool_call`` interception hooks,
    ``continue_prompt`` and ``provide_steering_inbox`` (agent events), and ``load_state``
    and ``provide_environment`` (turn state). ``running()`` owns the lifespan, store and
    steering inbox. Message-pipeline hooks such as ``build_prompt``,
    ``run_model``, ``save_state`` and outbound rendering only run in
    ``framework.process_inbound()``.
    """

    def __init__(self, framework: BubFramework | None = None, *, plugins: Iterable[object] | None = None) -> None:
        """Use the hooks of ``framework`` and ``plugins``.

        Without ``framework``, create one. It then registers just ``plugins`` when given,
        and otherwise loads Bub's builtin hooks and the installed plugins of the ``bub``
        entry-point group, as the ``bub`` command does. ``plugins`` given with a
        ``framework`` are registered on it.
        """
        if framework is None:
            from bub.framework import BubFramework

            framework = BubFramework()
            if plugins is None:
                framework.load_hooks()
        for plugin in plugins or ():
            framework.plugin_manager.register(plugin)
        self.framework = framework

    def running(self) -> contextlib.AbstractAsyncContextManager[contextlib.AsyncExitStack]:
        """Enter the framework's lifespan (``framework.running()``) around the agent's runs."""
        return self.framework.running()

    def components(self) -> AgentComponents:
        """Return the store, tape context and sidecars that hooks provide, to pass to ``Agent(...)``.

        A component no hook provides is left out, so the agent keeps its default. The
        store is only available inside ``running()``.
        """
        components: AgentComponents = {}
        if (store := self.framework.get_tape_store()) is not None:
            components["store"] = store
        if self.framework.plugin_manager.hook.build_tape_context.get_hookimpls():
            components["context"] = self.framework.build_tape_context()
        if sidecars := self.framework.get_tape_sidecars():
            components["sidecars"] = sidecars
        return components

    def attach(self, agent: Agent, *, tools: bool = True) -> Agent:
        """Add the hooks' prompt section and tools to ``agent`` and handle its events with the hooks.

        The section joins the ``system_prompt`` hook results, low to high priority, after
        the agent's current sections. The ``provide_tools`` tools are added unless
        ``tools`` is false; a tool the agent already has by that name is kept. Attach
        before adding your own handlers so that they see the hooks' changes. Return ``agent``.
        """
        agent.system_prompt.add(self._system_prompt)
        if tools:
            for item in self.framework.get_tools():
                agent.tools.setdefault(item.name, item)
        self.framework.get_agent_hooks().subscribe(agent.events)
        agent.events.on(MessagesPending, self._drain_steering)
        agent.events.on(StepFinished, self._continue_prompt)
        return agent

    async def build_state(
        self, session_id: str, *, message: Envelope | None = None, agent: Agent | None = None
    ) -> TurnState:
        """Load a run's turn state through ``load_state`` and ``provide_environment`` hooks.

        ``message`` is the inbound message the hooks see; it defaults to an empty one.
        ``agent`` lets state recovery read that agent's store.
        """
        inbound = {} if message is None else normalize_envelope(message)
        if agent is not None:
            inbound["_runtime_agent"] = agent
        state = await self.framework.build_state(inbound, session_id)
        state.setdefault("session_id", session_id)
        return state

    def _system_prompt(self, context: PromptContext) -> str:
        return self.framework.get_system_prompt(prompt=context.prompt, state=context.state)

    async def _drain_steering(self, event: MessagesPending) -> None:
        inbox = self.framework.get_steering_inbox()
        if inbox is None:
            return
        state = event.tape.context.state
        envelopes = await inbox.drain_messages(state)
        event.messages.extend(
            await asyncio.gather(*[
                self.framework.build_prompt(message, session_id=field_of(message, "session_id"), state=state)
                for message in envelopes
            ])
        )

    async def _continue_prompt(self, event: StepFinished) -> None:
        if event.status != "continue":
            return
        continuation = await self.framework.continue_prompt(prompt=event.prompt, tape=event.tape, state=event.stream)
        if continuation is not None:
            event.next_prompt = [continuation]
