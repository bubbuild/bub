from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import republic

from bub.builtin.steering import InMemorySteeringInbox
from bub.core import Agent
from bub.core.events import LLMCallRequest, LLMCallResult
from bub.core.store import InMemoryTapeStore
from bub.framework import BubFramework
from bub.hooks import hookimpl
from bub.hooks.agent import AgentPlugins
from bub.turn import TurnState
from tests.model_fakes import ProviderService, chat_events, sse


class SessionPlugin:
    def __init__(self) -> None:
        self.store = InMemoryTapeStore()
        self.inbox = InMemorySteeringInbox()
        self.continued: list[Any] = []

    @hookimpl
    def system_prompt(self, prompt: Any, state: TurnState) -> str:
        return f"Serve {state['tenant']}."

    @hookimpl
    def load_state(self, message: Any, session_id: str) -> TurnState:
        return {"tenant": "acme"}

    @hookimpl
    def provide_tape_store(self) -> InMemoryTapeStore:
        return self.store

    @hookimpl
    def provide_steering_inbox(self) -> InMemorySteeringInbox:
        return self.inbox

    @hookimpl
    async def after_llm_call(self, request: LLMCallRequest, result: LLMCallResult, state: TurnState) -> None:
        if result.text == "one":
            await self.inbox.enqueue_message({"session_id": "s1", "content": "late"}, state)

    @hookimpl
    def continue_prompt(self, prompt: Any, tape: Any, state: Any) -> None:
        self.continued.append(prompt)


@pytest.mark.asyncio
async def test_agent_plugins_connect_loop_hooks_to_core_agent(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(sse(chat_events("one")))
    provider_service.reply(sse(chat_events("two")))
    plugin = SessionPlugin()
    framework = BubFramework(config_file=tmp_path / "bub.yml")
    framework.plugin_manager.register(plugin, name="session")
    plugins = AgentPlugins(framework)

    async with framework.running(), provider_service.client() as client:
        components = plugins.components()
        assert components == {"store": plugin.store}
        agent = plugins.attach(
            Agent(
                model=republic.get_model("openai:test", http_client=client, api_format="chat", api_key="test"),
                system_prompt=[plugins.system_prompt, "Be brief."],
                **components,
            )
        )
        state = await plugins.build_state("s1", agent=agent)
        await plugin.inbox.enqueue_message({"session_id": "s1", "content": "early"}, state)
        async with agent.run("hello", tape="s1", state=state) as stream:
            events = [event async for event in stream]

    assert [event.data.get("text") for event in events if event.kind == "final"] == ["one", "two"]
    first, second = provider_service.body(0), provider_service.body(1)
    assert first["messages"] == [
        {"role": "system", "content": "Serve acme.\n\nBe brief."},
        {"role": "user", "content": "early"},
        {"role": "user", "content": "hello"},
    ]
    assert second["messages"][-2:] == [
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "late"},
    ]
    assert plugin.continued == ["hello"]
    assert plugin.inbox.message_count(state) == 0
    assert state["session_id"] == "s1"
    assert (await agent.tape("s1").read_messages())[-1] == republic.assistant("two")


def test_agent_plugins_leave_out_components_no_hook_provides(tmp_path: Path) -> None:
    plugins = AgentPlugins(BubFramework(config_file=tmp_path / "bub.yml"))

    assert plugins.components() == {}
