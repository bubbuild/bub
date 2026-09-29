from pathlib import Path
from unittest.mock import Mock

import pytest

from bub.builtin import Agent
from bub.builtin.tools import resolve_tool_names, run_subagent, show_help
from bub.framework import BubFramework
from bub.store import InMemoryTapeStore
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub.tape import Tape
from bub.tools import Tool, ToolContext


def _reply() -> AsyncStreamEvents:
    async def events():
        yield StreamEvent("text", {"delta": "done"})
        yield StreamEvent("final", {"text": "done"})

    return AsyncStreamEvents(events())


@pytest.fixture
def framework(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BubFramework:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    return framework


@pytest.mark.asyncio
async def test_sdk_command_prefix_is_instance_local(framework: BubFramework, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_COMMAND_PREFIX", "!")
    lookup = Tool.from_callable(lambda: "found", name="sdk.lookup")
    configured = Agent(framework, tools=[lookup], skill_dirs=[])
    custom = Agent(framework, tools=[lookup, show_help], skill_dirs=[], command_prefix="::")
    assert configured.command_prefix == "!"
    assert configured.settings.command_prefix == custom.settings.command_prefix == "!"

    for agent, prefix in [(configured, "!"), (custom, "::")]:
        agent.model_runner.run = Mock(side_effect=AssertionError("commands must not invoke the model"))
        stream = await agent.run_stream(session_id="sdk", prompt=f" {prefix}sdk.lookup ")
        assert [event async for event in stream][-1].data["text"] == "found"
        tape = agent.tape.session_tape("sdk", framework.workspace)
        entries = list(await tape.store.fetch_all(tape.query().kinds("event")))
        command = next(entry.payload["data"] for entry in entries if entry.payload.get("name") == "command")
        assert command["raw"] == "sdk.lookup"

    stream = await custom.run_stream(session_id="sdk", prompt="::help")
    assert "::bash.output" in [event async for event in stream][-1].data["text"]

    custom.model_runner.run = Mock(side_effect=lambda **kwargs: _reply())
    for prompt in [",sdk.lookup", "!sdk.lookup"]:
        stream = await custom.run_stream(session_id="sdk", prompt=prompt)
        assert [event.kind async for event in stream] == ["text", "final"]
        assert custom.model_runner.run.call_args.kwargs["prompt"] == prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("has_saved_state", [False, True])
@pytest.mark.parametrize("override", [False, True])
async def test_sdk_recovers_only_its_store_and_honors_explicit_overrides(
    framework: BubFramework, has_saved_state: bool, override: bool
) -> None:
    builtin = framework.plugin_manager.get_plugin("builtin")
    builtin_tape = builtin._get_agent().tape.session_tape("shared", framework.workspace)
    await builtin_tape.append_event("model_switch", {"model": "test:other"})
    await builtin_tape.append_event("reasoning_effort_switch", {"reasoning_effort": "low"})

    agent = Agent(framework, tools=[], tape_store=InMemoryTapeStore(), skill_dirs=[])
    tape = agent.tape.session_tape("shared", framework.workspace)
    if has_saved_state:
        await tape.append_event("model_switch", {"model": "test:saved"})
        await tape.append_event("reasoning_effort_switch", {"reasoning_effort": "high"})

    runner = Mock(side_effect=lambda **kwargs: _reply())
    agent.model_runner.run = runner
    stream = await agent.run_stream(
        session_id="shared",
        prompt="hello",
        model="test:explicit" if override else None,
        reasoning_effort="medium" if override else None,
    )
    assert [event.kind async for event in stream] == ["text", "final"]
    call = runner.call_args.kwargs
    expected_model = "test:saved" if has_saved_state else agent.settings.model
    assert call["model"] == ("test:explicit" if override else expected_model)
    state = call["tape"].context.state
    assert state.get("reasoning_effort") == ("medium" if override else "high" if has_saved_state else None)
    assert state["_runtime_agent"] is agent


def test_instance_tool_names_resolve_aliases_and_exclusions_from_one_index() -> None:
    names = ["sdk.lookup", "sdk.other"]
    assert resolve_tool_names([" SDK_LOOKUP "], all_names=iter(names)) == {"sdk.lookup"}
    assert resolve_tool_names(exclude=["SDK_OTHER"], all_names=iter(names)) == {"sdk.lookup"}
    assert resolve_tool_names(["sdk_lookup"], exclude=["sdk.lookup"], all_names=names) == set()
    assert resolve_tool_names(all_names=[]) == set()
    with pytest.raises(ValueError, match="bash"):
        resolve_tool_names(["bash"], all_names=names)
    with pytest.raises(ValueError, match="bash"):
        resolve_tool_names(exclude=["bash"], all_names=names)


@pytest.mark.asyncio
async def test_agent_allowlist_accepts_unregistered_instance_tool(framework: BubFramework) -> None:
    tool = Tool.from_callable(lambda: "found", name="sdk.lookup")
    agent = Agent(framework, tools=[tool], tape_store=InMemoryTapeStore(), skill_dirs=[])
    runner = Mock(side_effect=lambda **kwargs: _reply())
    agent.model_runner.run = runner
    stream = await agent.run_stream(session_id="sdk", prompt="lookup", allowed_tools=[" SDK_LOOKUP "])
    assert [event.kind async for event in stream] == ["text", "final"]
    assert [tool.name for tool in runner.call_args.kwargs["tools"]] == ["sdk_lookup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_tools", [None, ["SDK_LOOKUP"]])
async def test_subagent_uses_parent_instance_tools(framework: BubFramework, allowed_tools: list[str] | None) -> None:
    tool = Tool.from_callable(lambda: "found", name="sdk.lookup")
    agent = Agent(framework, tools=[tool, run_subagent], tape_store=InMemoryTapeStore(), skill_dirs=[])
    runner = Mock(side_effect=lambda **kwargs: _reply())
    agent.model_runner.run = runner
    tape: Tape = agent.tape.session_tape("parent", framework.workspace)
    context = ToolContext(
        tape=tape,
        state={"_runtime_agent": agent, "session_id": "parent", "_runtime_workspace": str(framework.workspace)},
    )
    result = await run_subagent.run(prompt="lookup", allowed_tools=allowed_tools, context=context)
    assert result == "done"
    assert [tool.name for tool in runner.call_args.kwargs["tools"]] == ["sdk_lookup"]
