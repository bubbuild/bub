from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from any_llm.types.completion import ChatCompletion

from bub.builtin.agent import Agent
from bub.builtin.codemode import run_code
from bub.framework import BubFramework
from bub.tape import Tape
from bub.tools import Tool


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [False, True])
@pytest.mark.parametrize("reverse_providers", [False, True])
async def test_discovery_precedence_is_independent_of_provider_order_for_native_and_code_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code_mode: bool, reverse_providers: bool
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    direct = Tool.from_callable(lambda: "direct", name="direct")
    denied = Tool.from_callable(lambda: "denied", name="denied")
    pending = Tool.from_callable(lambda: "pending", name="catalog.pending")
    calls: list[str] = []

    def lookup(name: str) -> str:
        calls.append(name)
        return f"Hello {name}"

    supplied = Tool.from_callable(lookup, name="catalog.lookup")

    async def select(tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
        return [item for item in tools if item is not pending], "Use catalog_lookup to greet the requested person."

    async def guide(tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
        return tools, "Keep the greeting brief."

    requests: list[dict[str, Any]] = []

    class Provider:
        SUPPORTS_COMPLETION_STREAMING = False

        async def acompletion(self, **kwargs: Any) -> ChatCompletion:
            requests.append(kwargs)
            message: dict[str, Any] = {"role": "assistant", "content": "Hello Ada"}
            if len(requests) == 1:
                name = "run_code" if code_mode else "catalog_lookup"
                arguments = {"code": "print(await tools.catalog_lookup(name='Ada'))"} if code_mode else {"name": "Ada"}
                message = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "lookup",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            return ChatCompletion.model_validate({
                "id": "reply",
                "model": "test-model",
                "created": 0,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                        "message": message,
                    }
                ],
            })

    monkeypatch.setattr("bub.builtin.model_runner.AnyLLM.create", lambda *args, **kwargs: Provider())
    agent = Agent(framework, tools=[direct, run_code], skill_dirs=[])
    agent.tool_sources["earlier"] = {supplied.name: Tool.from_callable(lambda: "wrong", name=supplied.name)}
    agent.tool_sources["later"] = {supplied.name: supplied, denied.name: denied, pending.name: pending}
    agent.tool_providers = [guide, select] if reverse_providers else [select, guide]
    stream = await agent.run_stream(
        session_id="catalog",
        prompt="Greet Ada.",
        model="openrouter:test-model",
        allowed_tools=["direct", "catalog_lookup", "catalog_pending", "run_code"],
        state={"code_mode": code_mode},
    )
    async for _ in stream:
        pass
    assert calls == ["Ada"]
    definitions = {item["function"]["name"]: item["function"] for item in requests[0]["tools"]}
    assert definitions.keys() == ({"run_code"} if code_mode else {"direct", "catalog_lookup"})
    if code_mode:
        stub = next((tmp_path / "codemode").rglob("*.pyi")).read_text()
        assert "catalog_pending" not in stub
    system = "\n".join(message["content"] for message in requests[0]["messages"] if message["role"] == "system")
    guidance = (
        ["Keep the greeting brief.", "Use catalog_lookup"]
        if reverse_providers
        else ["Use catalog_lookup", "Keep the greeting brief."]
    )
    assert system.index(guidance[0]) < system.index(guidance[1])
    assert any(
        "Hello Ada" in message.get("content", "") for message in requests[1]["messages"] if message["role"] == "tool"
    )
