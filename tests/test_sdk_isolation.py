from pathlib import Path

import pytest

from bub.builtin import BubAgent
from bub.builtin.codemode import run_code, set_code_mode
from bub.builtin.model import BubModel
from bub.builtin.tools import resolve_tool_names, run_subagent, show_help
from bub.core import Agent as CoreAgent
from bub.core.store import InMemoryTapeStore
from bub.core.tape import Tape, session_tape_name
from bub.core.tools import Tool, ToolContext
from bub.framework import BubFramework
from bub.hooks import hookimpl
from bub.streaming import AsyncStreamEvents, StreamEvent


def _reply() -> AsyncStreamEvents:
    async def events():
        yield StreamEvent("text", {"delta": "done"})
        yield StreamEvent("final", {"text": "done"})

    return AsyncStreamEvents(events())


def capture_steps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[CoreAgent, Tape, BubModel]]:
    """Replace model steps with a fixed reply and record the core agents, tapes and models that ran them."""
    steps: list[tuple[CoreAgent, Tape, BubModel]] = []

    def run_once(self: CoreAgent, prompt: object, *, model: BubModel, tape: Tape, **_: object) -> AsyncStreamEvents:
        steps.append((self, tape, model))
        return _reply()

    monkeypatch.setattr(CoreAgent, "run_once", run_once)
    return steps


async def step_tool_names(step: tuple[CoreAgent, Tape, BubModel]) -> list[str]:
    agent, tape, _ = step
    return [tool.name for tool in await agent.prepare_tools(tape, None)]


@pytest.fixture
def framework(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BubFramework:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    return framework


@pytest.mark.asyncio
async def test_sdk_agents_execute_only_their_own_command_prefix(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_COMMAND_PREFIX", "!")
    monkeypatch.setattr("bub.core.agent.Agent.run_once", lambda *args, **kwargs: _reply())
    custom = BubAgent(framework, tools=[show_help], skill_dirs=[], command_prefix="::")
    configured = BubAgent(framework, tools=[show_help], skill_dirs=[])

    for agent, prompt, expected in [
        (custom, " ::help ", "Commands use '::'"),
        (configured, "!help", "Commands use '!'"),
        (custom, "!help", "done"),
        (custom, ",help", "done"),
    ]:
        stream = await agent.run_stream(session_id="sdk", prompt=prompt)
        output = "".join([event.data["delta"] async for event in stream if event.kind == "text"])
        assert output.startswith(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [",", "::"])
@pytest.mark.parametrize("enabled", [True, False])
async def test_prefixed_code_mode_command_is_restored_by_a_new_agent(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch, prefix: str, enabled: bool
) -> None:
    store = InMemoryTapeStore()
    tools = [set_code_mode, run_code]
    agent = BubAgent(framework, tools=tools, tape_store=store, skill_dirs=[], command_prefix=prefix)
    tape = agent.tape(session_tape_name("sdk", framework.workspace))
    await tape.append_event("code_mode_switch", {"code_mode": not enabled})

    stream = await agent.run_stream(session_id="sdk", prompt=f"{prefix}code_mode enable={str(enabled).lower()}")
    events = [event async for event in stream]
    status = "enabled" if enabled else "disabled"
    assert any(
        event.data.get("delta") == f"Session code mode {status} (applies from the next turn)." for event in events
    )

    restored = BubAgent(framework, tools=tools, tape_store=store, skill_dirs=[], command_prefix=prefix)
    steps = capture_steps(monkeypatch)
    stream = await restored.run_stream(session_id="sdk", prompt="hello")

    assert [event.kind async for event in stream] == ["text", "final"]
    assert steps[-1][1].context.state["code_mode"] is enabled
    assert await step_tool_names(steps[-1]) == (["run_code"] if enabled else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["sdk.lookup", "echo hello"])
async def test_prefixed_commands_render_structured_results(framework: BubFramework, command: str) -> None:
    async def lookup(**kwargs: object) -> dict[str, str]:
        return {"output": "found"}

    command_tool = Tool(
        name="sdk.lookup" if command == "sdk.lookup" else "bash",
        handler=lookup,
        renderer=lambda result: result["output"],
    )
    agent = BubAgent(framework, tools=[command_tool], skill_dirs=[], command_prefix="::")

    stream = await agent.run_stream(session_id="sdk", prompt=f"::{command}")
    output = "".join([event.data["delta"] async for event in stream if event.kind == "text"])

    assert output == "found"
    tape = agent.tape(session_tape_name("sdk", framework.workspace))
    events = list(await tape.store.fetch_all(tape.query().kinds("event")))
    recorded = [entry.payload["data"] for entry in events if entry.payload.get("name") == "command"]
    assert len(recorded) == 1
    assert recorded[0]["output"] == "found"
    assert recorded[0]["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_saved_state", [False, True])
@pytest.mark.parametrize("override", [False, True])
async def test_sdk_recovers_only_its_store_and_honors_explicit_overrides(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch, has_saved_state: bool, override: bool
) -> None:
    builtin = framework.plugin_manager.get_plugin("builtin")
    builtin_tape = builtin._get_agent().tape(session_tape_name("shared", framework.workspace))
    await builtin_tape.append_event("model_switch", {"model": "test:other"})
    await builtin_tape.append_event("reasoning_effort_switch", {"reasoning_effort": "low"})

    agent = BubAgent(framework, tools=[], tape_store=InMemoryTapeStore(), skill_dirs=[])
    tape = agent.tape(session_tape_name("shared", framework.workspace))
    if has_saved_state:
        await tape.append_event("model_switch", {"model": "test:saved"})
        await tape.append_event("reasoning_effort_switch", {"reasoning_effort": "high"})

    steps = capture_steps(monkeypatch)
    stream = await agent.run_stream(
        session_id="shared",
        prompt="hello",
        model="test:explicit" if override else None,
        reasoning_effort="medium" if override else None,
    )
    assert [event.kind async for event in stream] == ["text", "final"]
    expected_model = "test:saved" if has_saved_state else agent.settings.model
    assert steps[-1][2].name == ("test:explicit" if override else expected_model)
    expected_effort = "medium" if override else "high" if has_saved_state else None
    assert steps[-1][2].options.get("reasoning_effort") == expected_effort
    state = steps[-1][1].context.state
    assert state.get("reasoning_effort") == expected_effort
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
async def test_agent_allowlist_accepts_unregistered_instance_tool(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = Tool.from_callable(lambda: "found", name="sdk.lookup")
    agent = BubAgent(framework, tools=[tool], tape_store=InMemoryTapeStore(), skill_dirs=[])
    steps = capture_steps(monkeypatch)
    stream = await agent.run_stream(session_id="sdk", prompt="lookup", allowed_tools=[" SDK_LOOKUP "])
    assert [event.kind async for event in stream] == ["text", "final"]
    assert await step_tool_names(steps[-1]) == ["sdk_lookup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed_tools", [None, ["SDK_LOOKUP"]])
async def test_subagent_uses_parent_instance_tools(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch, allowed_tools: list[str] | None
) -> None:
    tool = Tool.from_callable(lambda: "found", name="sdk.lookup")
    agent = BubAgent(framework, tools=[tool, run_subagent], tape_store=InMemoryTapeStore(), skill_dirs=[])
    steps = capture_steps(monkeypatch)
    tape: Tape = agent.tape(session_tape_name("parent", framework.workspace))
    context = ToolContext(
        tape=tape,
        state={"_runtime_agent": agent, "session_id": "parent", "_runtime_workspace": str(framework.workspace)},
    )
    result = await run_subagent.run(prompt="lookup", allowed_tools=allowed_tools, context=context)
    assert result["output"] == "done"
    assert run_subagent.render(result) == "done"
    assert await step_tool_names(steps[-1]) == ["sdk_lookup"]


@pytest.mark.asyncio
async def test_builtin_channels_create_the_agent_with_the_running_store(framework: BubFramework) -> None:
    store = InMemoryTapeStore()

    class StorePlugin:
        @hookimpl
        def provide_tape_store(self) -> InMemoryTapeStore:
            return store

    framework.plugin_manager.register(StorePlugin(), name="store")
    cli = framework.get_channels(lambda message: None)["cli"]  # Channels exist before running().

    async with framework.running():
        await cli._agent.tape("early").append_event("probe", {})  # type: ignore[attr-defined]

    assert store.list_tapes() == ["early"]
