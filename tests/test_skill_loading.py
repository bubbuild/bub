from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from any_llm.types.completion import ChatCompletion

from bub.builtin.agent import Agent
from bub.framework import BubFramework
from bub.tools import Tool


@pytest.mark.asyncio
@pytest.mark.parametrize("multimodal", [False, True])
@pytest.mark.parametrize("allowed", [True, False])
async def test_explicit_skill_scope_and_history_survive_tool_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multimodal: bool, allowed: bool
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    skill_dir = tmp_path / ".agents/skills/review"
    skill_dir.mkdir(parents=True)
    body = "Keep every evidence reference."
    (skill_dir / "SKILL.md").write_text(f"---\nname: review\ndescription: Review records.\n---\n{body}\n")
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    requests: list[dict[str, Any]] = []
    calls: list[str] = []

    def lookup() -> str:
        calls.append("lookup")
        return "evidence found"

    class Provider:
        SUPPORTS_COMPLETION_STREAMING = False

        async def acompletion(self, **kwargs: Any) -> ChatCompletion:
            requests.append(kwargs)
            message: dict[str, Any] = {"role": "assistant", "content": "reviewed"}
            if len(requests) == 1:
                message = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "lookup",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
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

    provider = Provider()
    monkeypatch.setattr("bub.builtin.model_runner.AnyLLM.create", lambda *args, **kwargs: provider)
    agent = Agent(framework, tools=[Tool.from_callable(lookup)], skill_dirs=[skill_dir.parent])
    image = {"type": "image_url", "image_url": {"url": "https://example.test/evidence.png"}}
    prompt: str | list[dict] = "$REVIEW Check the records."
    if multimodal:
        prompt = [{"type": "text", "text": prompt}, image]
    for current in (prompt, "Continue the review."):
        stream = await agent.run_stream(
            session_id="review",
            prompt=current,
            model="openrouter:test-model",
            allowed_skills=["REVIEW"] if allowed else [],
            state={"_runtime_workspace": str(tmp_path)},
        )
        async for _ in stream:
            pass

    assert calls == ["lookup"]
    systems = [request["messages"][0]["content"] for request in requests]
    assert all(system == systems[0] and body not in system for system in systems)
    for request in requests:
        users = [message["content"] for message in request["messages"] if message["role"] == "user"]
        assert json.dumps(users).count(body) == int(allowed)
    if multimodal:
        first_user = next(message for message in requests[0]["messages"] if message["role"] == "user")
        assert image in first_user["content"]
    assert any(
        "evidence found" in message.get("content", "")
        for message in requests[-1]["messages"]
        if message["role"] == "tool"
    )
