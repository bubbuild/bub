from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from any_llm.constants import LLMProvider
from any_llm.providers.openai.base import BaseOpenAIProvider
from any_llm.types.completion import ChatCompletionChunk

from bub.builtin.context import default_tape_context
from bub.builtin.model_runner import ModelRunner, is_context_length_error
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.builtin.tape import Tape
from bub.builtin.tool_output import cap_tool_result
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.tools import Tool

SPILL_PATH_RE = re.compile(r"\[full output saved to: (?P<path>.+)\]")


def test_cap_tool_result_passes_through_small_and_non_string(tmp_path: Path) -> None:
    assert cap_tool_result("small", run_id="r", index=0, limit=1024, spill_dir=tmp_path) == "small"
    assert cap_tool_result({"k": "v"}, run_id="r", index=0, limit=8, spill_dir=tmp_path) == {"k": "v"}
    big = "x" * 5000
    # limit <= 0 disables capping entirely.
    assert cap_tool_result(big, run_id="r", index=0, limit=0, spill_dir=tmp_path) == big
    assert not list(tmp_path.iterdir())


def test_cap_tool_result_truncates_and_spills_full_output(tmp_path: Path) -> None:
    original = "A" * 8000  # single oversized line, like a minified bundle
    limit = 1024

    capped = cap_tool_result(original, run_id="run-abc", index=2, limit=limit, spill_dir=tmp_path)

    assert isinstance(capped, str)
    assert len(capped.encode("utf-8")) <= limit
    assert "[output truncated:" in capped
    match = SPILL_PATH_RE.search(capped)
    assert match is not None
    spill_path = Path(match.group("path"))
    assert spill_path.read_text(encoding="utf-8") == original


def test_cap_tool_result_does_not_split_multibyte_chars(tmp_path: Path) -> None:
    original = "你" * 4000  # 3 bytes each in UTF-8
    capped = cap_tool_result(original, run_id="run-utf8", index=0, limit=1024, spill_dir=tmp_path)

    assert isinstance(capped, str)
    # Decoding already happened without raising; the head must be valid UTF-8.
    assert len(capped.encode("utf-8")) <= 1024
    match = SPILL_PATH_RE.search(capped)
    assert match is not None
    assert Path(match.group("path")).read_text(encoding="utf-8") == original


class _FakeToolCallProvider(BaseOpenAIProvider):
    SUPPORTS_COMPLETION_STREAMING = True

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    async def acompletion(self, **_kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
        async def stream() -> AsyncIterator[ChatCompletionChunk]:
            yield ChatCompletionChunk.model_validate({
                "id": "chatcmpl_test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": self._tool_name, "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
            })

        return stream()


class _FakeOpenAIModelRunner(ModelRunner):
    def __init__(self, settings: AgentSettings, llm: _FakeToolCallProvider) -> None:
        super().__init__(settings)
        self._llm = llm

    def iter_llm_clients(self, model: str) -> Iterator[tuple[ModelCandidate, _FakeToolCallProvider]]:
        yield ModelCandidate(provider=LLMProvider.OPENAI, model_id=model, name=f"openai:{model}"), self._llm


@pytest.mark.asyncio
async def test_oversized_tool_result_is_bounded_in_next_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    limit = 1024
    original = "B" * (4 * 1024 * 1024)  # 4 MB single-line output

    def bigtool() -> str:
        return original

    tool = Tool.from_callable(bigtool, name="bigtool")
    store = InMemoryTapeStore()
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(store), default_tape_context()).scoped("test-tape")
    runner = _FakeOpenAIModelRunner(
        AgentSettings.model_construct(
            model="openai:gpt-test", max_tokens=100, model_timeout_seconds=None, max_tool_result_bytes=limit
        ),
        _FakeToolCallProvider(tool_name="bigtool"),
    )

    await tape.ensure_bootstrap_anchor()
    events = [
        event async for event in runner.run(tape=tape, model="gpt-test", tools=[tool], system_prompt=None, prompt="go")
    ]

    tool_result_events = [event for event in events if event.kind == "tool_result"]
    assert len(tool_result_events) == 1
    streamed = tool_result_events[0].data["tool_results"][0]
    assert len(streamed.encode("utf-8")) <= limit

    # The reconstructed next-turn request must carry the bounded result, not 4 MB.
    messages = await tape.read_messages()
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    content = tool_messages[0]["content"]
    assert len(content.encode("utf-8")) <= limit
    assert "[output truncated:" in content

    match = SPILL_PATH_RE.search(content)
    assert match is not None
    spill_path = Path(match.group("path"))
    assert spill_path.read_text(encoding="utf-8") == original


def test_413_is_treated_as_a_context_overflow_for_auto_handoff() -> None:
    body = "<html><title>413 Request Entity Too Large</title></html>"
    assert is_context_length_error(body) is True
