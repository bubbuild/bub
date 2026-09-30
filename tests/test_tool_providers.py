from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from any_llm.types.completion import ChatCompletion

from bub.builtin.agent import Agent
from bub.framework import BubFramework
from bub.tape import Tape
from bub.tools import Tool


@pytest.mark.asyncio
async def test_provider_tools_and_prompt_reach_the_model_and_use_normal_invocation_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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

    supplied = Tool.from_callable(lookup, name="provider.lookup")
    scopes: list[set[str]] = []

    async def provide(tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
        scopes.append({tool.name for tool in tools})
        return [*tools, supplied], "Use provider_lookup to greet the requested person."

    requests: list[dict[str, Any]] = []

    class Provider:
        SUPPORTS_COMPLETION_STREAMING = False

        async def acompletion(self, **kwargs: Any) -> ChatCompletion:
            requests.append(kwargs)
            message: dict[str, Any] = {"role": "assistant", "content": "Hello Ada"}
            if len(requests) == 1:
                message = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "lookup",
                            "type": "function",
                            "function": {"name": "provider_lookup", "arguments": json.dumps({"name": "Ada"})},
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
    agent = Agent(framework, tools=[direct, denied], skill_dirs=[])
    agent.tool_providers.append(provide)
    stream = await agent.run_stream(
        session_id="provider",
        prompt="Greet Ada.",
        model="openrouter:test-model",
        allowed_tools=["direct"],
    )
    events = [event async for event in stream]
    assert any(event.data.get("text") == "Hello Ada" for event in events if event.kind == "final")
    assert scopes == [{"direct"}, {"direct"}]
    assert calls == ["Ada"]
    definitions = {item["function"]["name"]: item["function"] for item in requests[0]["tools"]}
    assert definitions.keys() == {"direct", "provider_lookup"}
    assert definitions["provider_lookup"]["parameters"] == supplied.parameters
    assert any(
        "Use provider_lookup" in message["content"]
        for message in requests[0]["messages"]
        if message["role"] == "system"
    )
    assert any(
        message.get("content") == "Hello Ada" for message in requests[1]["messages"] if message["role"] == "tool"
    )
