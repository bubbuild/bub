from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from bub.builtin.agent import BubAgent
from bub.builtin.settings import AgentSettings
from bub.builtin.subagents import (
    SUBAGENT_TOOLS,
    SubagentEvent,
    SubagentFinished,
    SubagentOutput,
    SubAgentResult,
    SubagentStarted,
    run_subagent,
    subagent_cancel,
    subagent_manager,
    subagent_send,
    subagent_wait,
)
from bub.builtin.tools import ALLOWED_TOOLS_STATE_KEY
from bub.core.events import ToolCallStarted
from bub.core.store import InMemoryTapeStore
from bub.core.tape import session_tape_name
from bub.core.tools import Tool, ToolContext, tool
from bub.framework import BubFramework
from bub.streaming import AsyncStreamEvents, StreamEvent
from tests.model_fakes import ProviderService

PARENT_KEY = {"session_id": "parent", "_runtime_thread_id": "thread-1"}


def call(name: str, call_id: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments or {})}


@tool(name="tests.lookup")
def lookup() -> str:
    """Look something up."""
    return "found"


@tool(name="tests.other")
def other() -> str:
    """Another tool."""
    return "other"


class FakeContext:
    """Minimal ToolContext stand-in for argument plumbing tests."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.tape = None
        self.events = None
        self.step_messages: list[Any] = []


class FakeAgent:
    """Records child runs; ``block`` holds each run until ``release`` is set."""

    def __init__(self, *tools: Tool, block: bool = False, error: Exception | None = None) -> None:
        self.tools = {item.name: item for item in (lookup, other, *SUBAGENT_TOOLS, *tools)}
        self.calls: list[dict[str, Any]] = []
        self.block = block
        self.error = error
        self.release = asyncio.Event()

    async def run_stream(self, **kwargs: Any) -> AsyncStreamEvents:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

        async def iterator() -> AsyncIterator[StreamEvent]:
            if self.block:
                await self.release.wait()
            yield StreamEvent("text", {"delta": "agent result"})
            yield StreamEvent("final", {"ok": True, "text": "agent result"})

        return AsyncStreamEvents(iterator())


@pytest_asyncio.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_transport: ProviderService
) -> AsyncIterator[BubAgent]:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    async with framework.running():
        agent = BubAgent(
            framework, tools=[lookup, other, *SUBAGENT_TOOLS], tape_store=InMemoryTapeStore(), skill_dirs=[]
        )
        agent.settings = AgentSettings(model="openai:test", api_key="unused", client_args={"api_format": "chat"})
        yield agent


def parent_context(agent: BubAgent, **state: Any) -> ToolContext:
    workspace = agent.framework.workspace
    return ToolContext(
        tape=agent.tape(session_tape_name("parent", workspace)),
        state={"_runtime_agent": agent, "_runtime_workspace": str(workspace), **PARENT_KEY, **state},
        events=agent.events,
    )


async def run_parent(agent: BubAgent, prompt: str) -> None:
    state = {"_runtime_workspace": str(agent.framework.workspace), "_runtime_thread_id": "thread-1"}
    stream = await agent.run_stream(session_id="parent", prompt=prompt, state=state)
    async for _ in stream:
        pass


async def run_child(context: Any, **arguments: Any) -> SubAgentResult:
    """Start a subagent and wait for its result."""
    started = await run_subagent.run(context=context, **arguments)
    [result] = await subagent_wait.run(task_ids=[started["task_id"]], timeout_seconds=30, context=context)
    return result


def requests_ending_with(provider: ProviderService, text: str) -> list[dict[str, Any]]:
    """Request bodies whose last message is a user message containing ``text``.

    A parent run and its subagents call the model concurrently, so tests find a request
    by what it asks rather than by its position.
    """
    bodies = [provider.body(index) for index in range(len(provider.requests))]
    return [
        body
        for body in bodies
        if body["messages"][-1]["role"] == "user" and text in json.dumps(body["messages"][-1]["content"])
    ]


def system_prompt(body: dict[str, Any]) -> str:
    return body["messages"][0]["content"]


def reply_texts(provider: ProviderService, count: int) -> None:
    """Queue interchangeable plain answers for requests whose order is not fixed."""
    for _ in range(count):
        provider.reply_chat("noted")


@pytest.mark.asyncio
async def test_subagent_returns_a_task_at_once() -> None:
    agent = FakeAgent(block=True)
    context = FakeContext({"_runtime_agent": agent})

    started = await run_subagent.run(prompt="task", context=context)

    assert started["status"] in ("queued", "running")
    assert started["output"] == ""
    assert "running in the background" in run_subagent.render(started)
    agent.release.set()
    [result] = await subagent_wait.run(task_ids=[started["task_id"]], context=context)
    assert result["status"] == "completed"
    assert result["output"] == "agent result"


@pytest.mark.asyncio
async def test_run_waits_for_its_subagents_before_ending(
    runtime: BubAgent, provider_transport: ProviderService
) -> None:
    provider_transport.reply_chat(calls=[call("subagent", "c1", {"prompt": "child task"})])
    reply_texts(provider_transport, 3)
    started: list[SubagentStarted] = []
    runtime.events.on(SubagentStarted, started.append)

    await run_parent(runtime, "delegate")

    [task] = [event.task for event in started]
    assert task.done
    assert task.delivered
    [last] = requests_ending_with(provider_transport, "<subagent-notification")
    assert f'task_id="{task.task_id}" status="completed"' in str(last["messages"][-1]["content"])
    messages = await runtime.tape(session_tape_name("parent", runtime.framework.workspace)).read_messages()
    assert any("<subagent-notification" in json.dumps(message.to_dict()) for message in messages)
    inbox = runtime.framework.get_steering_inbox()
    assert inbox is not None
    assert await inbox.drain_messages(dict(PARENT_KEY)) == []


@pytest.mark.asyncio
async def test_subagent_leaves_parent_steering_messages_to_the_parent(
    runtime: BubAgent, provider_transport: ProviderService
) -> None:
    provider_transport.reply_chat(calls=[call("subagent", "c1", {"prompt": "child task"})])
    reply_texts(provider_transport, 3)

    @runtime.events.on(ToolCallStarted)
    async def steer_parent(event: ToolCallStarted) -> None:
        if event.call.tool == "subagent":
            await runtime.framework.steer_message(
                message={"content": "user follow-up", "session_id": "parent"},
                session_id="parent",
                state=dict(PARENT_KEY),
            )

    await run_parent(runtime, "delegate")

    [child] = requests_ending_with(provider_transport, "child task")
    assert "user follow-up" not in json.dumps(child)
    assert requests_ending_with(provider_transport, "user follow-up")


@pytest.mark.asyncio
async def test_subagent_returns_only_its_final_answer(runtime: BubAgent, provider_transport: ProviderService) -> None:
    provider_transport.reply_chat("let me look it up", calls=[call("tests_lookup", "l1")])
    provider_transport.reply_chat("the answer")

    result = await run_child(parent_context(runtime), prompt="find it")

    assert result["status"] == "completed"
    assert result["output"] == "the answer"
    assert run_subagent.render(result) == "the answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["fork", "inherit"])
async def test_forked_subagent_sees_the_current_turn_and_is_discarded(
    runtime: BubAgent, provider_transport: ProviderService, session: str
) -> None:
    provider_transport.reply_chat(calls=[call("subagent", "c1", {"prompt": "summarize it", "session": session})])
    reply_texts(provider_transport, 3)

    await run_parent(runtime, "parent question")

    [child] = requests_ending_with(provider_transport, "summarize it")
    assert "parent question" in json.dumps(child)
    messages = await runtime.tape(session_tape_name("parent", runtime.framework.workspace)).read_messages()
    user_texts = [json.dumps(message.to_dict()) for message in messages if message.role == "user"]
    assert any("parent question" in text for text in user_texts)
    assert not any("summarize it" in text for text in user_texts)


@pytest.mark.asyncio
async def test_child_state_gets_its_own_thread_and_drops_parent_scope() -> None:
    agent = FakeAgent()
    state = {"_runtime_agent": agent, **PARENT_KEY, ALLOWED_TOOLS_STATE_KEY: ["tests.lookup"], "extra": "val"}

    result = await run_child(FakeContext(state), prompt="task")

    child = agent.calls[0]
    assert child["session_id"] == f"temp/{result['task_id']}"
    assert child["state"]["session_id"] == child["session_id"]
    assert child["state"]["_runtime_thread_id"] == f"subagent:{result['task_id']}"
    assert ALLOWED_TOOLS_STATE_KEY not in child["state"]
    assert child["state"]["extra"] == "val"


@pytest.mark.asyncio
async def test_child_tools_stay_within_the_parent_allowlist() -> None:
    agent = FakeAgent()
    state = {"_runtime_agent": agent, **PARENT_KEY, ALLOWED_TOOLS_STATE_KEY: ["tests.lookup", "subagent"]}

    await run_child(FakeContext(state), prompt="task")
    assert agent.calls[0]["allowed_tools"] == {"tests.lookup"}

    with pytest.raises(ValueError, match="tests_other"):
        await run_subagent.run(prompt="task", allowed_tools=["tests_other"], context=FakeContext(state))


@pytest.mark.asyncio
async def test_child_cannot_start_or_manage_subagents() -> None:
    agent = FakeAgent()

    await run_child(FakeContext({"_runtime_agent": agent}), prompt="task", allowed_tools=[])

    allowed = agent.calls[0]["allowed_tools"]
    assert {"tests.lookup", "tests.other"} <= allowed
    assert not any(name == "subagent" or name.startswith("subagent.") for name in allowed)


@pytest.mark.asyncio
async def test_child_skills_stay_within_the_parent_allowlist() -> None:
    agent = FakeAgent()
    state = {"_runtime_agent": agent, "allowed_skills": ["review"]}

    await run_child(FakeContext(state), prompt="task")
    await run_child(FakeContext(state), prompt="task", allowed_skills=["Review", "deploy"])

    assert [child["allowed_skills"] for child in agent.calls] == [["review"], ["review"]]


@pytest.mark.asyncio
async def test_unknown_allowed_tools_are_rejected_before_running() -> None:
    agent = FakeAgent()

    with pytest.raises(ValueError, match="tests_missing_tool"):
        await run_subagent.run(
            prompt="task", allowed_tools=[" tests_missing_tool "], context=FakeContext({"_runtime_agent": agent})
        )

    assert agent.calls == []


@pytest.mark.asyncio
async def test_failing_child_reports_failed_status() -> None:
    agent = FakeAgent(error=RuntimeError("child exploded"))

    result = await run_child(FakeContext({"_runtime_agent": agent}), prompt="task")

    assert result["status"] == "failed"
    assert result["errors"] == ["child exploded"]


@pytest.mark.asyncio
async def test_subagent_ending_after_its_run_notifies_the_session_through_steering(
    runtime: BubAgent, provider_transport: ProviderService
) -> None:
    provider_transport.reply_chat("background done")

    result = await run_subagent.run(prompt="task", context=parent_context(runtime))
    task = subagent_manager.get(result["task_id"])
    await task._runner

    assert task.delivered
    inbox = runtime.framework.get_steering_inbox()
    assert inbox is not None
    [message] = await inbox.drain_messages(dict(PARENT_KEY))
    assert f'task_id="{result["task_id"]}" status="completed"' in message.content
    assert "background done" in message.content


@pytest.mark.asyncio
async def test_waiting_on_a_subagent_delivers_its_result_without_a_notification(
    runtime: BubAgent, provider_transport: ProviderService
) -> None:
    provider_transport.reply_chat("background done")
    context = parent_context(runtime)

    started = await run_subagent.run(prompt="task", context=context)
    [result] = await subagent_wait.run(task_ids=[started["task_id"]], timeout_seconds=5, context=context)
    await subagent_manager.get(started["task_id"])._runner

    assert result["status"] == "completed"
    assert result["output"] == "background done"
    inbox = runtime.framework.get_steering_inbox()
    assert inbox is not None
    assert await inbox.drain_messages(dict(PARENT_KEY)) == []


@pytest.mark.asyncio
async def test_wait_without_ids_covers_unread_tasks_of_the_session() -> None:
    agent = FakeAgent()
    context = FakeContext({"_runtime_agent": agent, "session_id": "wait-session"})
    first = await run_subagent.run(prompt="one", context=context)
    second = await run_subagent.run(prompt="two", context=context)

    results = await subagent_wait.run(context=context)
    assert [result["task_id"] for result in results] == [first["task_id"], second["task_id"]]
    assert await subagent_wait.run(context=context) == []


@pytest.mark.asyncio
async def test_wait_returns_running_tasks_after_its_timeout() -> None:
    agent = FakeAgent(block=True)
    context = FakeContext({"_runtime_agent": agent})
    started = await run_subagent.run(prompt="task", context=context)

    [result] = await subagent_wait.run(task_ids=[started["task_id"]], timeout_seconds=0, context=context)

    assert result["status"] in ("queued", "running")
    agent.release.set()
    await subagent_manager.get(started["task_id"])._runner


@pytest.mark.asyncio
async def test_cancel_stops_a_subagent() -> None:
    agent = FakeAgent(block=True)
    context = FakeContext({"_runtime_agent": agent})
    started = await run_subagent.run(prompt="task", context=context)

    [result] = await subagent_cancel.run(task_id=started["task_id"], context=context)

    assert result["status"] == "cancelled"
    assert "was cancelled" in subagent_cancel.render([result])


@pytest.mark.asyncio
async def test_cancel_session_stops_only_that_sessions_tasks() -> None:
    agent = FakeAgent(block=True)
    mine = await run_subagent.run(prompt="task", context=FakeContext({"_runtime_agent": agent, "session_id": "mine"}))
    theirs = await run_subagent.run(
        prompt="task", context=FakeContext({"_runtime_agent": agent, "session_id": "theirs"})
    )

    assert await subagent_manager.cancel_session("mine") == 1

    assert subagent_manager.get(mine["task_id"]).status == "cancelled"
    assert subagent_manager.get(theirs["task_id"]).status in ("queued", "running")
    agent.release.set()
    await subagent_manager.get(theirs["task_id"])._runner


@pytest.mark.asyncio
async def test_subagent_times_out() -> None:
    agent = FakeAgent(block=True)

    result = await run_child(FakeContext({"_runtime_agent": agent}), prompt="task", timeout_seconds=1)

    assert result["status"] == "failed"
    assert result["errors"] == ["timed out after 1 seconds"]


@pytest.mark.asyncio
async def test_concurrency_limit_queues_extra_subagents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_SUBAGENT_MAX_CONCURRENCY", "1")
    agent = FakeAgent(block=True)
    context = FakeContext({"_runtime_agent": agent})
    first = await run_subagent.run(prompt="one", context=context)
    second = await run_subagent.run(prompt="two", context=context)
    for _ in range(5):
        await asyncio.sleep(0)

    assert subagent_manager.get(first["task_id"]).status == "running"
    assert subagent_manager.get(second["task_id"]).status == "queued"
    agent.release.set()
    results = await subagent_wait.run(task_ids=[first["task_id"], second["task_id"]], context=context)
    assert [result["status"] for result in results] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_send_steers_a_running_subagent(runtime: BubAgent, provider_transport: ProviderService) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @tool(name="tests.block")
    async def block() -> str:
        entered.set()
        await release.wait()
        return "unblocked"

    runtime.tools[block.name] = block
    provider_transport.reply_chat(calls=[call("tests_block", "b1")])
    provider_transport.reply_chat("ok")
    context = parent_context(runtime)

    started = await run_subagent.run(prompt="task", context=context)
    await entered.wait()
    reply = await subagent_send.run(task_id=started["task_id"], message="extra details", context=context)
    release.set()
    await subagent_manager.get(started["task_id"])._runner

    assert "Message queued" in reply
    assert requests_ending_with(provider_transport, "extra details")


@pytest.mark.asyncio
async def test_send_continues_a_finished_named_session(runtime: BubAgent, provider_transport: ProviderService) -> None:
    provider_transport.reply_chat("first answer")
    provider_transport.reply_chat("second answer")
    context = parent_context(runtime)

    first = await run_child(context, prompt="first question", session="notes/1")
    started = await subagent_send.run(task_id=first["task_id"], message="second question", context=context)
    [second] = await subagent_wait.run(task_ids=[started["task_id"]], context=context)

    assert second["output"] == "second answer"
    assert second["task_id"] != first["task_id"]
    assert second["session_id"] == "notes/1"
    [continued] = requests_ending_with(provider_transport, "second question")
    assert "first question" in json.dumps(continued)


@pytest.mark.asyncio
async def test_send_rejects_a_finished_temporary_session() -> None:
    context = FakeContext({"_runtime_agent": FakeAgent()})
    finished = await run_child(context, prompt="task")

    with pytest.raises(ValueError, match="temporary session is gone"):
        await subagent_send.run(task_id=finished["task_id"], message="more", context=context)


@pytest.mark.asyncio
async def test_subagent_publishes_its_lifecycle_events(runtime: BubAgent, provider_transport: ProviderService) -> None:
    provider_transport.reply_chat("child answer")
    seen: list[SubagentEvent] = []
    runtime.events.on(SubagentEvent, seen.append)

    result = await run_child(parent_context(runtime), prompt="task")

    assert isinstance(seen[0], SubagentStarted)
    assert isinstance(seen[-1], SubagentFinished)
    assert {type(event) for event in seen[1:-1]} == {SubagentOutput}
    assert [event.event.kind for event in seen[1:-1] if isinstance(event, SubagentOutput)][-1] == "final"
    assert seen[-1].task.task_id == result["task_id"]
    assert seen[-1].task.status == "completed"
    assert seen[-1].state["session_id"] == "parent"


@pytest.mark.asyncio
async def test_failing_event_handler_does_not_fail_the_subagent(
    runtime: BubAgent, provider_transport: ProviderService
) -> None:
    provider_transport.reply_chat("child answer")

    def broken(event: SubagentEvent) -> None:
        raise RuntimeError("observer bug")

    runtime.events.on(SubagentEvent, broken)

    result = await run_child(parent_context(runtime), prompt="task")

    assert result["output"] == "child answer"


@pytest.mark.asyncio
async def test_profile_sets_the_child_model_tools_and_instructions(
    runtime: BubAgent, provider_transport: ProviderService
) -> None:
    profiles = runtime.framework.workspace / ".agents" / "agents"
    profiles.mkdir(parents=True)
    (profiles / "reviewer.md").write_text(
        "---\ndescription: Reviews code changes\nmodel: openai:review-model\ntools: [tests.lookup]\n---\n"
        "Only report findings.\n",
        encoding="utf-8",
    )
    provider_transport.reply_chat(calls=[call("subagent", "c1", {"prompt": "check it", "agent": "Reviewer"})])
    reply_texts(provider_transport, 3)

    await run_parent(runtime, "review please")

    parent = provider_transport.body(0)
    assert "<available_subagents>" in system_prompt(parent)
    assert "- reviewer: Reviews code changes" in system_prompt(parent)
    [child] = requests_ending_with(provider_transport, "check it")
    assert child["model"] == "review-model"
    assert "Only report findings." in system_prompt(child)
    assert "<available_subagents>" not in system_prompt(child)
    assert [item["function"]["name"] for item in child["tools"]] == ["tests_lookup"]


@pytest.mark.asyncio
async def test_unknown_profile_is_rejected(runtime: BubAgent) -> None:
    with pytest.raises(ValueError, match="unknown subagent profile 'ghost'"):
        await run_subagent.run(prompt="task", agent="ghost", context=parent_context(runtime))


@pytest.mark.asyncio
async def test_subagent_task_records_the_tool_call_that_started_it(
    runtime: BubAgent, provider_transport: ProviderService
) -> None:
    provider_transport.reply_chat(calls=[call("subagent", "call-42", {"prompt": "child task"})])
    reply_texts(provider_transport, 3)
    started: list[SubagentStarted] = []
    runtime.events.on(SubagentStarted, started.append)

    await run_parent(runtime, "delegate")

    assert [event.task.parent_call_id for event in started] == ["call-42"]
