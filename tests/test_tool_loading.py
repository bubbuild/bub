from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from any_llm.types.completion import ChatCompletion

from bub.builtin.agent import Agent
from bub.framework import BubFramework
from bub.tools import Tool


def _completion(message: dict[str, Any]) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "reply",
        "model": "test-model",
        "created": 0,
        "object": "chat.completion",
        "choices": [
            {"index": 0, "finish_reason": "tool_calls" if "tool_calls" in message else "stop", "message": message}
        ],
    })


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    }


def _tool_names(request: dict[str, Any]) -> list[str]:
    return [item["function"]["name"] for item in request.get("tools") or []]


def _system_prompt(request: dict[str, Any]) -> str:
    return "\n".join(message["content"] for message in request["messages"] if message["role"] == "system")


@pytest.fixture
def framework(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BubFramework:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    return framework


def _scripted_provider(monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    class Provider:
        SUPPORTS_COMPLETION_STREAMING = False

        async def acompletion(self, **kwargs: Any) -> ChatCompletion:
            requests.append(kwargs)
            return _completion(replies[len(requests) - 1])

    monkeypatch.setattr("bub.builtin.model_runner.AnyLLM.create", lambda *args, **kwargs: Provider())
    return requests


@pytest.mark.asyncio
async def test_deferred_tool_is_loaded_through_tool_describe_and_persists_on_tape(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def lookup(name: str) -> str:
        calls.append(name)
        return f"Hello {name}"

    direct = Tool.from_callable(lambda: "direct", name="direct")
    supplied = Tool.from_callable(lookup, name="provider.lookup", deferred=True)
    other = Tool.from_callable(lambda: "other", name="other", deferred=True)
    requests = _scripted_provider(
        monkeypatch,
        [
            _tool_call("describe", "tool_describe", {"names": ["provider_lookup"]}),
            _tool_call("lookup", "provider_lookup", {"name": "Ada"}),
            {"role": "assistant", "content": "Hello Ada"},
            {"role": "assistant", "content": "Again"},
        ],
    )
    agent = Agent(framework, tools=[direct, supplied, other], skill_dirs=[])

    stream = await agent.run_stream(session_id="loading", prompt="Greet Ada.", model="openrouter:test-model")
    events = [event async for event in stream]

    assert any(event.data.get("text") == "Hello Ada" for event in events if event.kind == "final")
    assert calls == ["Ada"]
    assert _tool_names(requests[0]) == ["direct", "tool_describe"]
    assert _tool_names(requests[1]) == ["direct", "tool_describe", "provider_lookup"]
    describe_result = next(message for message in requests[1]["messages"] if message["role"] == "tool")
    assert json.loads(describe_result["content"])["tools"][0]["name"] == "provider_lookup"
    prompt = _system_prompt(requests[0])
    assert "<deferred_tools>" in prompt
    assert "- provider_lookup" in prompt
    assert "- other" in prompt

    stream = await agent.run_stream(session_id="loading", prompt="Again.", model="openrouter:test-model")
    _ = [event async for event in stream]
    assert _tool_names(requests[3]) == ["direct", "tool_describe", "provider_lookup"]


@pytest.mark.asyncio
async def test_allowed_tools_filter_covers_direct_and_deferred_tools(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch
) -> None:
    direct = Tool.from_callable(lambda: "direct", name="direct")
    denied = Tool.from_callable(lambda: "denied", name="denied")
    allowed_deferred = Tool.from_callable(lambda: "lookup", name="provider.lookup", deferred=True)
    denied_deferred = Tool.from_callable(lambda: "secret", name="secret", deferred=True)
    requests = _scripted_provider(
        monkeypatch,
        [
            _tool_call("describe", "tool_describe", {"names": ["provider_lookup", "secret"]}),
            {"role": "assistant", "content": "done"},
        ],
    )
    agent = Agent(framework, tools=[direct, denied, allowed_deferred, denied_deferred], skill_dirs=[])

    stream = await agent.run_stream(
        session_id="allowed",
        prompt="Load tools.",
        model="openrouter:test-model",
        allowed_tools=["direct", "provider_lookup"],
    )
    _ = [event async for event in stream]

    assert _tool_names(requests[0]) == ["direct", "tool_describe"]
    assert _tool_names(requests[1]) == ["direct", "tool_describe", "provider_lookup"]
    prompt = _system_prompt(requests[0])
    assert "- provider_lookup" in prompt
    assert "secret" not in prompt
    describe_result = next(message for message in requests[1]["messages"] if message["role"] == "tool")
    assert json.loads(describe_result["content"])["unknown"] == ["secret"]


@pytest.mark.asyncio
async def test_tool_describe_is_hidden_without_deferred_tools(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bub.builtin.tools import tool_describe

    direct = Tool.from_callable(lambda: "direct", name="direct")
    requests = _scripted_provider(monkeypatch, [{"role": "assistant", "content": "done"}])
    agent = Agent(framework, tools=[direct, tool_describe], skill_dirs=[])

    stream = await agent.run_stream(session_id="plain", prompt="Hi.", model="openrouter:test-model")
    _ = [event async for event in stream]

    assert _tool_names(requests[0]) == ["direct"]
    assert "<deferred_tools>" not in _system_prompt(requests[0])


@pytest.mark.asyncio
async def test_calling_unloaded_deferred_tool_returns_load_guidance(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    supplied = Tool.from_callable(lambda: calls.append("lookup") or "ok", name="provider.lookup", deferred=True)
    requests = _scripted_provider(
        monkeypatch,
        [_tool_call("lookup", "provider_lookup", {}), {"role": "assistant", "content": "done"}],
    )
    agent = Agent(framework, tools=[supplied], skill_dirs=[])

    stream = await agent.run_stream(session_id="unloaded", prompt="Look up.", model="openrouter:test-model")
    _ = [event async for event in stream]

    assert calls == []
    result = next(message for message in requests[1]["messages"] if message["role"] == "tool")
    assert result["content"] == "Tool `provider_lookup` is not loaded. Call `tool_describe` with its name first."


@pytest.mark.asyncio
async def test_code_mode_exposes_deferred_tools_to_code_without_loading(
    framework: BubFramework, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bub.builtin.codemode import CODE_TOOLS_STATE_KEY, run_code

    direct = Tool.from_callable(lambda: "direct", name="direct")
    supplied = Tool.from_callable(lambda: "lookup", name="provider.lookup", deferred=True)
    requests = _scripted_provider(monkeypatch, [{"role": "assistant", "content": "done"}])
    agent = Agent(framework, tools=[direct, supplied, run_code], skill_dirs=[])
    state: dict[str, Any] = {"code_mode": True, "_runtime_workspace": str(tmp_path)}

    stream = await agent.run_stream(session_id="code", prompt="Hi.", model="openrouter:test-model", state=state)
    _ = [event async for event in stream]

    assert _tool_names(requests[0]) == ["run_code"]
    assert [tool.name for tool in state[CODE_TOOLS_STATE_KEY]] == ["direct", "provider_lookup"]
    assert "<deferred_tools>" not in _system_prompt(requests[0])
