from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import republic

from bub.builtin.steering import InMemorySteeringInbox
from bub.core import Agent
from bub.core.events import LLMCallRequest, LLMCallResult
from bub.core.store import InMemoryTapeStore
from bub.core.tools import Tool, tool
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
    plugins = AgentPlugins(BubFramework(config_file=tmp_path / "bub.yml"), plugins=[plugin])

    async with plugins.running(), provider_service.client() as client:
        components = plugins.components()
        assert components == {"store": plugin.store}
        agent = plugins.attach(
            Agent(
                model=republic.get_model("openai:test", http_client=client, api_format="chat", api_key="test"),
                system_prompt="Be brief.",
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
        {"role": "system", "content": "Be brief.\n\nServe acme."},
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


def test_agent_plugins_create_a_framework_with_the_given_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BubFramework, "load_hooks", lambda self: pytest.fail("installed plugins loaded"))
    plugin = SessionPlugin()

    plugins = AgentPlugins(plugins=[plugin])

    assert plugins.framework.plugin_manager.get_plugins() == {plugin}


def test_agent_plugins_without_arguments_load_installed_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[BubFramework] = []
    monkeypatch.setattr(BubFramework, "load_hooks", lambda self: loaded.append(self))

    plugins = AgentPlugins()

    assert loaded == [plugins.framework]


class ToolsPlugin:
    def __init__(self, *tools: Tool) -> None:
        self.tools = list(tools)

    @hookimpl
    def provide_tools(self) -> list[Tool]:
        return self.tools


def test_agent_plugins_attach_hook_tools_but_not_decorated_ones(tmp_path: Path) -> None:
    @tool(name="tests.plugin_tool")
    def plugin_tool() -> str:
        return "plugin"

    @tool(name="tests.unprovided_tool")
    def unprovided_tool() -> str:
        return "unprovided"

    own = Tool(name="tests.own_tool", handler=lambda: "own")
    low = Tool(name="tests.plugin_tool", handler=lambda: "low")
    framework = BubFramework(config_file=tmp_path / "bub.yml")
    plugins = AgentPlugins(framework, plugins=[ToolsPlugin(low, own)])
    framework.plugin_manager.register(ToolsPlugin(plugin_tool))
    mine = Tool(name="tests.own_tool", handler=lambda: "mine")

    assert plugins.components() == {}
    agent = plugins.attach(Agent(tools=[mine]))
    assert agent.tools == {"tests.own_tool": mine, "tests.plugin_tool": plugin_tool}
    assert plugins.attach(Agent(), tools=False).tools == {}
