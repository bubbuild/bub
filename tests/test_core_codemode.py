from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import republic

from bub.core import Agent
from bub.core.codemode import CodeMode, default_model_resolver
from bub.core.events import ToolCallStarted
from bub.core.store import InMemoryTapeStore
from bub.core.tools import Tool, ToolContext, ToolExecutor
from bub.environment import LocalEnvironment
from tests.model_fakes import ProviderService, chat_events, sse, tool_events
from tests.test_core_boundary import APP_MODULES


def lookup(order_id: str) -> dict[str, Any]:
    """Look up an order."""
    return {"id": order_id, "total": 12.5}


def _run_code_call(code: str) -> object:
    arguments = json.dumps({"code": code})
    return sse(tool_events([{"index": 0, "id": "call-1", "function": {"name": "run_code", "arguments": arguments}}]))


def test_importing_core_codemode_loads_no_app_dependencies() -> None:
    script = (
        "import sys, bub.core.codemode\n"
        "loaded = {name.split('.')[0] for name in sys.modules}\n"
        f"print(sorted(loaded & set({APP_MODULES!r})))\n"
        "print(sorted(name for name in sys.modules if name.startswith(('bub.builtin', 'bub.framework'))))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == ["[]", "[]"]


@pytest.mark.asyncio
async def test_core_agent_calls_tools_from_code(provider_service: ProviderService, tmp_path: Path) -> None:
    provider_service.reply(_run_code_call("order = await tools.orders_get(order_id='A1')\nprint(order['total'])"))
    provider_service.reply(sse(chat_events("It costs 12.5.")))
    code_mode = CodeMode([Tool.from_callable(lookup, name="orders.get")], environment=LocalEnvironment(tmp_path))
    calls: list[tuple[str, bool]] = []

    async with provider_service.client() as client:
        model = republic.get_model("openai:test", http_client=client, api_format="chat", api_key="test")
        agent = Agent(
            store=InMemoryTapeStore(), tools=[code_mode.run_code], system_prompt=["Be brief.", code_mode.prompt]
        )
        agent.events.on(
            ToolCallStarted,
            lambda event: calls.append((event.call.tool, event.call.context.code_mode)),  # type: ignore[union-attr]
        )
        async with agent.run(["How much is order A1?"], model=model, tape="session") as stream:
            events = [event async for event in stream]

    assert events[-1].data == {"ok": True, "text": "It costs 12.5."}
    first, second = provider_service.body(0), provider_service.body(1)
    assert [tool["function"]["name"] for tool in first["tools"]] == ["run_code"]
    system_prompt = first["messages"][0]["content"]
    assert system_prompt.startswith("Be brief.\n\n<code_mode>")
    assert "async def orders_get(*, order_id: str) -> dict[str, Any]:" in system_prompt
    assert "async def attach_image(" in system_prompt
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call-1", "content": "12.5\n"}
    assert calls == [("run_code", False), ("orders_get", True)]


def test_code_mode_prompt_points_at_a_stub_file_in_stub_dir(tmp_path: Path) -> None:
    code_mode = CodeMode([Tool.from_callable(lookup)], stub_dir=tmp_path / "stubs", media_tools=False)

    prompt = code_mode.prompt(_prompt_context())

    assert prompt is not None
    stub_path = next((tmp_path / "stubs").iterdir())
    assert f"in the stub file: {stub_path}\n" in prompt
    assert "async def lookup(*, order_id: str)" in stub_path.read_text(encoding="utf-8")
    assert "attach_image" not in stub_path.read_text(encoding="utf-8").split("# `republic`")[0]


@pytest.mark.asyncio
async def test_code_mode_selects_tools_per_run_from_state(tmp_path: Path) -> None:
    code_mode = CodeMode(lambda state: state.get("code_tools"), environment=LocalEnvironment(tmp_path))
    tape = Agent().tape("session")

    assert code_mode.prompt(_prompt_context()) is None
    execution = await ToolExecutor().execute_many(
        [(code_mode.run_code, {"code": "print(1)"})], context=ToolContext(tape=tape)
    )
    assert execution.error is not None
    assert execution.error.message == "Code mode is not enabled for this run."

    state = {"code_tools": [Tool.from_callable(lookup)]}
    output = await code_mode.run_code.run(
        code="print((await tools.lookup(order_id='B2'))['id'])", context=ToolContext(tape=tape, state=state)
    )
    assert output == "B2\n"
    assert [tool.name for tool in code_mode.code_tools(state) or []] == [
        "lookup",
        "attach_image",
        "attach_audio",
        "attach_video",
    ]


def test_default_model_resolver_needs_a_spec_or_default() -> None:
    context = ToolContext(tape=Agent().tape("session"))

    assert default_model_resolver("openai:gpt-x")(None, context) == ("openai:gpt-x", {})
    assert default_model_resolver("openai:gpt-x")("anthropic:claude", context) == ("anthropic:claude", {})
    with pytest.raises(ValueError, match="No default model"):
        default_model_resolver()(None, context)


@pytest.mark.asyncio
async def test_code_republic_models_use_the_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(spec: str | None, context: ToolContext) -> tuple[str, dict[str, Any]]:
        return f"resolved:{spec}", {"api_key": "secret"}

    def get_model(spec: str, **kwargs: Any) -> Any:
        raise RuntimeError(f"{spec} {kwargs}")

    monkeypatch.setattr(republic, "get_model", get_model)
    code_mode = CodeMode(environment=LocalEnvironment(tmp_path), models=resolve)
    code = "try:\n    await republic.get_model('x').chat('hi')\nexcept Exception as exc:\n    print(exc)\n"

    output = await code_mode.run_code.run(code=code, context=ToolContext(tape=Agent().tape("session")))

    assert output == "[tool] republic.chat failed: RuntimeError: resolved:x {'api_key': 'secret'}\n"


def _prompt_context() -> Any:
    from bub.core import PromptContext

    agent = Agent()
    return PromptContext(agent=agent, tape=agent.tape("session"), prompt=[], tools=[], model="openai:test")
