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
async def test_catalog_tools_and_prompt_reach_the_model_within_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code_mode: bool
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    direct = Tool.from_callable(lambda: "direct", name="direct")
    denied = Tool.from_callable(lambda: "denied", name="denied")
    calls: list[str] = []

    def lookup(name: str) -> str:
        calls.append(name)
        return f"Hello {name}"

    supplied = Tool.from_callable(lookup, name="catalog.lookup")

    class Catalog:
        def __init__(self) -> None:
            self.tools = {supplied.name: supplied}

        async def prepare(self, tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
            tape.context.state["_runtime_agent"].tools[supplied.name] = supplied
            return tools, "Use catalog_lookup to greet the requested person."

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
    agent = Agent(framework, tools=[direct, denied, run_code], skill_dirs=[])
    agent.catalogs.insert(-1, Catalog())
    stream = await agent.run_stream(
        session_id="catalog",
        prompt="Greet Ada.",
        model="openrouter:test-model",
        allowed_tools=["direct", "catalog_lookup", "run_code"],
        state={"code_mode": code_mode},
    )
    async for _ in stream:
        pass
    assert calls == ["Ada"]
    definitions = {item["function"]["name"]: item["function"] for item in requests[0]["tools"]}
    assert definitions.keys() == ({"run_code"} if code_mode else {"direct", "catalog_lookup"})
    assert any(
        "Use catalog_lookup" in message["content"] for message in requests[0]["messages"] if message["role"] == "system"
    )
    assert any(
        "Hello Ada" in message.get("content", "") for message in requests[1]["messages"] if message["role"] == "tool"
    )
