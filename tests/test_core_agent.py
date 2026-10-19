from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import republic

import bub.core
from bub.core import Agent, PromptContext
from bub.core.events import (
    AgentEvent,
    ChatFinished,
    CommandFinished,
    EventBus,
    LLMCallDecision,
    LLMCallFinished,
    LLMCallStarted,
    MessagesPending,
    RunFinished,
    RunStarted,
    StepFinished,
    ToolCallDecision,
    ToolCallFinished,
    ToolCallStarted,
)
from bub.core.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.core.tools import Tool
from bub.errors import BubError
from tests.model_fakes import ProviderService, chat_events, sse, tool_events


def _model(client: Any) -> republic.ChatModel:
    return republic.get_model("openai:test", http_client=client, api_format="chat", api_key="test")


def _tool_call(name: str) -> object:
    return sse(tool_events([{"index": 0, "id": "call-1", "function": {"name": name, "arguments": "{}"}}]))


@pytest.mark.asyncio
async def test_core_agent_pending_messages_continue_the_run_with_next_prompt(
    provider_service: ProviderService,
) -> None:
    provider_service.reply(sse(chat_events("first")))
    provider_service.reply(sse(chat_events("second")))
    waiting: list[list[Any]] = [["early"]]
    finished: list[tuple[int, str, Any]] = []
    async with provider_service.client() as client:
        agent = Agent(model=_model(client))

        @agent.events.on(MessagesPending)
        def drain(event: MessagesPending) -> None:
            event.messages.extend(waiting)
            waiting.clear()

        @agent.events.on(ChatFinished)
        def arrive_late(event: ChatFinished) -> None:
            if event.response_text == "first":
                waiting.append(["late"])

        @agent.events.on(StepFinished)
        def follow_up(event: StepFinished) -> None:
            finished.append((event.step, event.status, event.prompt))
            if event.status == "continue":
                event.next_prompt = ["go on"]

        async with agent.run("hello", tape="session") as stream:
            events = [event async for event in stream]

    assert [event.data.get("text") for event in events if event.kind == "final"] == ["first", "second"]
    assert finished == [(1, "continue", ["hello"]), (2, "ok", ["go on"])]
    assert await agent.tape("session").read_messages() == [
        republic.user("early"),
        republic.user("hello"),
        republic.assistant("first"),
        republic.user("late"),
        republic.user("go on"),
        republic.assistant("second"),
    ]
    tape = agent.tape("session")
    entries = await tape.store.fetch_all(tape.query().kinds("event"))
    assert [entry.payload["data"]["status"] for entry in entries if entry.payload["name"] == "loop.step"] == [
        "continue",
        "ok",
    ]


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

        @agent.system_prompt.add
        def tools_section(context: PromptContext) -> str:
            prompts.append((context.prompt, context.state.get("user"), [tool.name for tool in context.tools]))
            assert context.model == "openai:test"
            assert context.available_tools == [read]
            return "Tools: " + ", ".join(tool.name for tool in context.tools)

        @agent.system_prompt.add
        async def empty_section(context: PromptContext) -> None:
            return None

        @agent.events.on(ToolCallStarted)
        def rewrite_path(event: ToolCallStarted) -> None:
            event.call = replace(event.call, arguments={"path": "README.md"})

        assert agent.system_prompt.add("Removed section.") == "Removed section."
        agent.system_prompt.remove("Removed section.")
        assert agent.system_prompt.sections == ("Be brief.", tools_section, empty_section)
        agent.events.on(AgentEvent, lambda event: seen.append(type(event).__name__))
        async with agent.run(
            ["What is this file?"], model=model, options={"max_tokens": 64}, tape="session", state={"user": "ada"}
        ) as stream:
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
        "MessagesPending",
        "LLMCallStarted",
        "LLMCallFinished",
        "ToolCallStarted",
        "ToolCallFinished",
        "ChatFinished",
        "StepFinished",
        "StepStarted",
        "MessagesPending",
        "LLMCallStarted",
        "LLMCallFinished",
        "ChatFinished",
        "MessagesPending",
        "StepFinished",
        "RunFinished",
    ]
    # The run's fork merged back into the store's tape.
    tape = agent.tape("session")
    roles = [message.role for message in await tape.read_messages()]
    assert roles[-4:] == ["user", "assistant", "tool", "assistant"]
    names = [entry.payload["name"] for entry in await tape.store.fetch_all(tape.query().kinds("event"))]
    assert names.index("loop.start") < names.index("loop.step.start") < names.index("run") < names.index("loop.finish")


@pytest.mark.asyncio
async def test_before_events_short_circuit_remaining_handlers(provider_service: ProviderService) -> None:
    provider_service.reply(_tool_call("secret"))
    later: list[str] = []
    finished: list[ChatFinished] = []

    async with provider_service.client() as client:
        model = _model(client)
        agent = Agent(store=InMemoryTapeStore(), tools=[Tool.from_callable(lambda: "x", name="secret")])
        agent.events.on(ToolCallStarted, lambda event: setattr(event, "decision", ToolCallDecision.deny("no secrets")))
        agent.events.on(ToolCallStarted, lambda event: later.append("tool"))
        agent.events.on(LLMCallStarted, lambda event: later.append("llm"))
        agent.events.on(ChatFinished, finished.append)
        async with agent.session("deny") as tape:
            events = [event async for event in agent.run_once(["go"], model=model, tape=tape)]

        result = next(event for event in events if event.kind == "tool_result").data["tool_results"][0]
        assert result["message"] == "no secrets"
        assert later == ["llm"]

        agent.events.on(
            LLMCallStarted, lambda event: setattr(event, "decision", LLMCallDecision.finish("budget exhausted"))
        )
        agent.events.on(LLMCallFinished, lambda event: later.append("after"))
        async with agent.run(["again"], model=model, tape="budget") as stream:
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
        agent.events.on(ToolCallFinished, lambda event: statuses.append(f"tool={event.result.result}"))
        agent.events.on(RunFinished, lambda event: statuses.append(f"run={event.status}:{event.steps}:{event.error}"))
        with pytest.raises(RuntimeError, match="max_steps_reached=1"):
            async with agent.run(["loop"], model=model, tape="steps") as stream:
                [event async for event in stream]
    assert statuses == ["model=openai:test", "tool=ok", "continue", "run=error:1:max_steps_reached=1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "expected_peak"), [(None, 3), (2, 2), (1, 1)])
async def test_max_tool_concurrency_limits_tool_calls_of_one_response(
    provider_service: ProviderService, limit: int | None, expected_peak: int
) -> None:
    calls = [
        {"index": i, "id": f"call-{i}", "function": {"name": "wait", "arguments": f'{{"n": {i}}}'}} for i in range(3)
    ]
    provider_service.reply(sse(tool_events(calls)))
    provider_service.reply(sse(chat_events("done")))
    running = 0
    peak = 0
    started: list[int] = []

    async def wait(n: int) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return "ok"

    async with provider_service.client() as client:
        agent = Agent(
            model=_model(client),
            tools=[Tool.from_callable(wait, name="wait")],
            max_tool_concurrency=limit,
        )
        agent.events.on(ToolCallStarted, lambda event: started.append(event.call.arguments["n"]))
        async with agent.run(["go"], tape="concurrency") as stream:
            [event async for event in stream]

    assert peak == expected_peak
    assert started == [0, 1, 2]


@pytest.mark.parametrize("limit", [0, -1])
def test_max_tool_concurrency_must_be_positive(limit: int) -> None:
    with pytest.raises(ValueError, match="max_tool_concurrency"):
        Agent(max_tool_concurrency=limit)


@pytest.mark.asyncio
async def test_redirected_call_reuses_the_provider_of_a_republic_model(provider_service: ProviderService) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        agent = Agent(store=InMemoryTapeStore())

        @agent.events.on(LLMCallStarted)
        def reroute(event: LLMCallStarted) -> None:
            event.request = replace(event.request, model="openai:other")

        async with agent.run(["hi"], model=_model(client), tape="redirect") as stream:
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


def test_tapes_share_the_agent_store() -> None:
    store = InMemoryTapeStore()
    agent = Agent(store=store)

    assert agent.tape("a").store is agent.tape("b").store
    assert isinstance(Agent().tape("a").store, AsyncTapeStoreAdapter)


@pytest.mark.asyncio
async def test_leaving_run_early_closes_the_stream_and_merges_the_fork(provider_service: ProviderService) -> None:
    provider_service.reply(sse(chat_events("hello there")))
    finished: list[RunFinished] = []
    async with provider_service.client() as client:
        agent = Agent()
        agent.events.on(RunFinished, finished.append)
        async with agent.run(["hi"], model=_model(client), tape="early") as stream:
            async for event in stream:
                if event.kind == "text":
                    break

    messages = await agent.tape("early").read_messages()
    assert [message.role for message in messages] == []
    names = [entry.payload["name"] for entry in await agent.tape("early").store.fetch_all(agent.tape("early").query())]
    assert "loop.start" in names
    assert "loop.finish" in names
    assert [(event.status, event.steps) for event in finished] == [("cancelled", 1)]


@pytest.mark.asyncio
async def test_run_finished_follows_a_run_closed_before_iteration(provider_service: ProviderService) -> None:
    finished: list[RunFinished] = []
    async with provider_service.client() as client:
        agent = Agent()
        agent.events.on(RunFinished, finished.append)
        async with agent.run(["hi"], model=_model(client), tape="unread"):
            pass

    assert [(event.status, event.steps, event.error) for event in finished] == [("cancelled", 0, None)]
    assert provider_service.requests == []


@pytest.mark.asyncio
async def test_commands_do_not_start_or_finish_a_run() -> None:
    seen: list[str] = []
    agent = Agent(tools=[Tool.from_callable(lambda: "tool ran", name="echo")], command_prefix=",")
    agent.events.on(AgentEvent, lambda event: seen.append(type(event).__name__))
    async with agent.run([",echo"], tape="command") as stream:
        [event async for event in stream]

    assert "RunStarted" not in seen
    assert "RunFinished" not in seen
    assert "CommandFinished" in seen


@pytest.mark.asyncio
async def test_commands_are_disabled_without_a_prefix(provider_service: ProviderService) -> None:
    provider_service.reply(sse(chat_events("model answer")))
    async with provider_service.client() as client:
        agent = Agent(tools=[Tool.from_callable(lambda: "tool ran", name="echo")])
        async with agent.run([",echo"], model=_model(client), tape="plain") as stream:
            events = [event async for event in stream]

    assert events[-1].data == {"ok": True, "text": "model answer"}
    assert provider_service.body()["messages"][-1] == {"role": "user", "content": ",echo"}


@pytest.mark.asyncio
async def test_runs_call_the_agent_model_unless_given_one(provider_service: ProviderService) -> None:
    provider_service.reply(sse(chat_events("default answer")))
    provider_service.reply(sse(chat_events("override answer")))

    async with provider_service.client() as client:
        default = republic.get_model("openai:default", http_client=client, api_format="chat", api_key="test")
        override = republic.get_model("openai:override", http_client=client, api_format="chat", api_key="test")
        agent = Agent(model=default)
        async with agent.run("hello", tape="plain") as stream:
            default_events = [event async for event in stream]
        async with agent.run("again", model=override, tape="plain") as stream:
            override_events = [event async for event in stream]

    assert default_events[-1].data == {"ok": True, "text": "default answer"}
    assert override_events[-1].data == {"ok": True, "text": "override answer"}
    assert [provider_service.body(index)["model"] for index in (0, 1)] == ["default", "override"]


@pytest.mark.asyncio
async def test_builtin_tools_are_sent_with_function_tools(provider_service: ProviderService) -> None:
    provider_service.reply(sse(chat_events("searched")))
    ping = Tool.from_callable(lambda: "pong", name="ping")
    native = republic.tools.NativeTool("chat", {"type": "custom_search"})

    async with provider_service.client() as client:
        agent = Agent(model=_model(client), tools=[ping, republic.tools.WebSearch(), native])
        async with agent.run("search", tape="builtin") as stream:
            events = [event async for event in stream]

    assert events[-1].data == {"ok": True, "text": "searched"}
    assert list(agent.tools) == ["ping"]
    assert agent.builtin_tools == [republic.tools.WebSearch(), native]
    body = provider_service.body()
    assert body["tools"][0]["function"]["name"] == "ping"
    assert body["tools"][1] == {"type": "custom_search"}
    assert "web_search_options" in body


@pytest.mark.asyncio
async def test_runs_without_a_model_raise_unless_they_are_commands() -> None:
    agent = Agent(tools=[Tool.from_callable(lambda: "pong", name="ping")], command_prefix="/")

    async with agent.run("/ping", tape="plain") as stream:
        events = [event async for event in stream]
    assert events[-1].data == {"text": "pong", "ok": True}

    with pytest.raises(ValueError, match="no model to run"):
        async with agent.run("hello", tape="plain") as stream:
            [event async for event in stream]


@pytest.mark.asyncio
async def test_command_tool_calls_emit_tool_events_that_can_deny_them() -> None:
    def greet(name: str, punctuation: str = "!") -> str:
        return f"hello {name}{punctuation}"

    agent = Agent(tools=[Tool.from_callable(greet, name="greet")], command_prefix="/")
    calls: list[tuple[str, dict[str, Any]]] = []

    @agent.events.on(ToolCallStarted)
    def deny_bob(event: ToolCallStarted) -> None:
        calls.append((event.call.tool, event.call.arguments))
        if event.call.arguments["name"] == "bob":
            event.decision = ToolCallDecision.deny("no bob")

    model = republic.get_model("openai:never-called", api_key="test")
    async with agent.run(["/greet ada ?"], model=model, tape="commands") as stream:
        events = [event async for event in stream]
    assert events[-1].data == {"text": "hello ada?", "ok": True}

    with pytest.raises(BubError, match="no bob"):
        async with agent.run(["/greet bob"], model=model, tape="commands") as stream:
            [event async for event in stream]
    with pytest.raises(ValueError, match="at most 2 positional arguments"):
        async with agent.run(["/greet ada ? extra"], model=model, tape="commands") as stream:
            [event async for event in stream]
    assert calls == [("greet", {"name": "ada", "punctuation": "?"}), ("greet", {"name": "bob"})]


@pytest.mark.asyncio
async def test_command_prompt_calls_the_tool_without_the_model() -> None:
    def greet(name: str, *, punctuation: str = "!") -> str:
        return f"hello {name}{punctuation}"

    agent = Agent(tools=[Tool.from_callable(greet, name="greet")], command_prefix="/")
    finished: list[CommandFinished] = []
    agent.events.on(CommandFinished, finished.append)
    model = republic.get_model("openai:never-called", api_key="test")
    async with agent.run(["  /greet ada punctuation=?"], model=model, tape="commands") as stream:
        events = [event async for event in stream]

    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": "hello ada?"}),
        ("final", {"text": "hello ada?", "ok": True}),
    ]
    tape = agent.tape("commands")
    (command,) = [
        entry.payload["data"]
        for entry in await tape.store.fetch_all(tape.query().kinds("event"))
        if entry.payload["name"] == "command"
    ]
    assert (command["name"], command["status"], command["output"]) == ("greet", "ok", "hello ada?")

    with pytest.raises(ValueError, match="bash tool is not available"):
        async with agent.run(["/unknown"], model=model, tape="commands") as stream:
            [event async for event in stream]
    assert [(event.line, event.name, event.status, event.output) for event in finished] == [
        ("greet ada punctuation=?", "greet", "ok", "hello ada?"),
        ("unknown", "unknown", "error", "bash tool is not available"),
    ]
