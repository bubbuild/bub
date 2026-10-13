from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import republic

import bub.core
from bub.core import (
    AfterLlmCall,
    AfterToolCall,
    Agent,
    AgentEvent,
    BeforeLlmCall,
    BeforeToolCall,
    ChatCompleted,
    EventBus,
    LlmCallDecision,
    PromptContext,
    RunStarted,
    StepFinished,
    Tool,
    ToolCallDecision,
)
from bub.core.store import InMemoryTapeStore
from tests.model_fakes import ProviderService, chat_events, sse, tool_events


def _model(client: Any) -> republic.ChatModel:
    return republic.get_model("openai:test", http_client=client, api_format="chat", api_key="test")


def _tool_call(name: str) -> object:
    return sse(tool_events([{"index": 0, "id": "call-1", "function": {"name": name, "arguments": "{}"}}]))


@pytest.mark.asyncio
async def test_core_agent_runs_tool_loop_and_emits_events(provider_service: ProviderService) -> None:
    provider_service.reply(_tool_call("fs_read"))
    provider_service.reply(sse(chat_events("It is a readme.")))
    prompts: list[object] = []
    seen: list[str] = []
    store = InMemoryTapeStore()
    read = Tool.from_callable(lambda path: f"contents of {path}", name="fs.read")
    async with provider_service.client() as client:
        model = _model(client)
        agent = Agent(store=store, tools=[read], system_prompt="Be brief.")

        @agent.add_system_prompt
        def tools_section(context: PromptContext) -> str:
            prompts.append((context.prompt, context.state.get("user"), [tool.name for tool in context.tools]))
            assert context.model == "openai:test"
            assert context.available_tools == [read]
            return "Tools: " + ", ".join(tool.name for tool in context.tools)

        @agent.add_system_prompt
        async def empty_section(context: PromptContext) -> None:
            return None

        @agent.events.on(BeforeToolCall)
        def rewrite_path(event: BeforeToolCall) -> None:
            event.call = replace(event.call, arguments={"path": "README.md"})

        assert agent.add_system_prompt("Removed section.") == "Removed section."
        agent.remove_system_prompt("Removed section.")
        assert agent.system_prompt.sections == ("Be brief.", tools_section, empty_section)
        agent.events.on(AgentEvent, lambda event: seen.append(type(event).__name__))
        stream = await agent.run(
            ["What is this file?"], model=model, options={"max_tokens": 64}, tape="session", state={"user": "ada"}
        )
        events = [event async for event in stream]

    assert events[-1].data == {"ok": True, "text": "It is a readme."}
    first, second = provider_service.body(0), provider_service.body(1)
    assert first["messages"][0] == {"role": "system", "content": "Be brief.\n\nTools: fs_read"}
    assert [tool["function"]["name"] for tool in first["tools"]] == ["fs_read"]
    assert first["max_completion_tokens"] == 64
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call-1", "content": "contents of README.md"}
    assert prompts == [(["What is this file?"], "ada", ["fs_read"]), ([], "ada", ["fs_read"])]
    assert seen == [
        "RunStarted",
        "StepStarted",
        "BeforeLlmCall",
        "AfterLlmCall",
        "BeforeToolCall",
        "AfterToolCall",
        "ChatCompleted",
        "StepFinished",
        "StepStarted",
        "BeforeLlmCall",
        "AfterLlmCall",
        "ChatCompleted",
        "StepFinished",
    ]
    # The run's fork merged back into the store's tape.
    tape = agent.tape("session")
    roles = [message.role for message in await tape.read_messages()]
    assert roles[-4:] == ["user", "assistant", "tool", "assistant"]
    names = [entry.payload["name"] for entry in await tape.store.fetch_all(tape.query().kinds("event"))]
    assert names.index("loop.start") < names.index("loop.step.start") < names.index("run")


@pytest.mark.asyncio
async def test_before_events_short_circuit_remaining_handlers(provider_service: ProviderService) -> None:
    provider_service.reply(_tool_call("secret"))
    later: list[str] = []
    finished: list[ChatCompleted] = []

    async with provider_service.client() as client:
        model = _model(client)
        agent = Agent(store=InMemoryTapeStore(), tools=[Tool.from_callable(lambda: "x", name="secret")])
        agent.events.on(BeforeToolCall, lambda event: setattr(event, "decision", ToolCallDecision.deny("no secrets")))
        agent.events.on(BeforeToolCall, lambda event: later.append("tool"))
        agent.events.on(BeforeLlmCall, lambda event: later.append("llm"))
        agent.events.on(ChatCompleted, finished.append)
        async with agent.session("deny") as tape:
            events = [event async for event in agent.run_once(["go"], model=model, tape=tape)]

        result = next(event for event in events if event.kind == "tool_result").data["tool_results"][0]
        assert result["message"] == "no secrets"
        assert later == ["llm"]

        agent.events.on(
            BeforeLlmCall, lambda event: setattr(event, "decision", LlmCallDecision.finish("budget exhausted"))
        )
        agent.events.on(AfterLlmCall, lambda event: later.append("after"))
        stream = await agent.run(["again"], model=model, tape="budget")
        events = [event async for event in stream]

    assert events[-1].data == {"ok": True, "text": "budget exhausted"}
    assert later == ["llm", "llm"]
    assert finished[-1].response_text == "budget exhausted"
    assert len(provider_service.requests) == 1


@pytest.mark.asyncio
async def test_removed_handler_no_longer_receives_events() -> None:
    bus = EventBus()
    received: list[str] = []

    @bus.on(AgentEvent)
    async def record(event: AgentEvent) -> None:
        received.append(type(event).__name__)

    def direct(event: AgentEvent) -> None:
        received.append("direct")

    assert bus.on(AgentEvent, direct) is direct
    await bus.emit(AgentEvent(state={}))
    bus.off(AgentEvent, record)
    bus.off(AgentEvent, record)
    await bus.emit(AgentEvent(state={}))

    assert received == ["AgentEvent", "direct", "direct"]


@pytest.mark.asyncio
async def test_core_agent_stops_at_max_steps(provider_service: ProviderService) -> None:
    provider_service.reply(_tool_call("noop"))
    statuses: list[str] = []
    async with provider_service.client() as client:
        model = _model(client)
        agent = Agent(
            store=InMemoryTapeStore(),
            tools=[Tool.from_callable(lambda: "ok", name="noop")],
            max_steps=1,
        )
        agent.events.on(StepFinished, lambda event: statuses.append(event.status))
        agent.events.on(RunStarted, lambda event: statuses.append(f"model={event.model}"))
        agent.events.on(AfterToolCall, lambda event: statuses.append(f"tool={event.result.result}"))
        stream = await agent.run(["loop"], model=model, tape="steps")
        with pytest.raises(RuntimeError, match="max_steps_reached=1"):
            [event async for event in stream]
    assert statuses == ["model=openai:test", "tool=ok", "continue"]


@pytest.mark.asyncio
async def test_redirected_call_reuses_the_provider_of_a_republic_model(provider_service: ProviderService) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        agent = Agent(store=InMemoryTapeStore())

        @agent.events.on(BeforeLlmCall)
        def reroute(event: BeforeLlmCall) -> None:
            event.request = replace(event.request, model="openai:other")

        stream = await agent.run(["hi"], model=_model(client), tape="redirect")
        events = [event async for event in stream]

    assert events[-1].data == {"ok": True, "text": "done"}
    assert provider_service.body()["model"] == "other"


def test_core_does_not_depend_on_settings_or_plugins() -> None:
    forbidden = ("bub.builtin", "bub.configure", "bub.framework", "bub.hooks", "bub.channels", "pluggy")
    for path in Path(bub.core.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            assert not [name for name in names if name.startswith(forbidden)], f"{path.name} imports {names}"


def test_store_function_is_resolved_on_first_tape_use() -> None:
    calls: list[str] = []
    store = InMemoryTapeStore()

    def resolve() -> InMemoryTapeStore:
        calls.append("resolve")
        return store

    agent = Agent(store=resolve)
    assert calls == []
    assert agent.tape("a").store is agent.tape("b").store
    assert calls == ["resolve"]
