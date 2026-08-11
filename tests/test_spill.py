"""Behavior and regression tests for tool-output spilling.

These are acceptance tests for what a user actually observes:
- an oversized tool result never re-enters a model request in full (no 413),
- the full payload stays reachable through the spill tape and read_tool_result,
- small results and tool errors are untouched,
- a spill write failure degrades to the original result (never a failed turn).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import bub.builtin.tools as builtin_tools
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings
from bub.builtin.spill import (
    MAX_READ_LINES,
    SPILL_TAPE,
    handle_key,
    read_slice,
    read_spilled,
    spill_ref,
)
from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import Tape
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeContext
from bub.tools import Tool, ToolContext, ToolExecutor


def _make_context(tmp_path: Path, store: Any) -> ToolContext:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    return ToolContext(
        tape=tape, run_id="run-1", state={"_runtime_workspace": str(tmp_path), "_runtime_spill_store": store}
    )


@pytest.fixture
def spill_store() -> AsyncTapeStoreAdapter:
    return AsyncTapeStoreAdapter(InMemoryTapeStore())


@pytest.mark.asyncio
async def test_oversized_tool_result_is_spilled_and_readable(tmp_path: Path, spill_store: Any) -> None:
    """An oversized bash result becomes a ref in the result, and the full payload reads back."""
    big = "line-%04d\n" * 3000  # ~30k chars -> well over the 4096-token threshold
    executor = ToolExecutor()
    context = _make_context(tmp_path, spill_store)

    tool_obj = Tool(name="bash", handler=lambda cmd: big, description="", parameters={})
    execution = await executor.execute_async([(tool_obj, {"cmd": "x"})], context=context)

    result = execution.tool_results[0]
    assert isinstance(result, str)
    assert "tool output spilled" in result
    assert "handle:" in result

    handle = result.split("handle: ")[1].split("]")[0].strip()
    full = await read_spilled(store=spill_store, handle=handle)
    assert full == big


@pytest.mark.asyncio
async def test_spill_ref_is_small_and_self_describing(tmp_path: Path, spill_store: Any) -> None:
    """The ref a model sees is a short marker with handle, shape, and preview — not the payload."""
    big = "x" * 200_000
    executor = ToolExecutor()
    context = _make_context(tmp_path, spill_store)
    tool_obj = Tool(name="bash", handler=lambda cmd: big, description="", parameters={})

    execution = await executor.execute_async([(tool_obj, {"cmd": "x"})], context=context)

    ref = execution.tool_results[0]
    assert "200,000 chars" in ref
    assert "read_tool_result(" in ref
    assert len(ref) < 2_000  # the ref itself is tiny


@pytest.mark.asyncio
async def test_small_tool_result_is_untouched(tmp_path: Path, spill_store: Any) -> None:
    context = _make_context(tmp_path, spill_store)
    executor = ToolExecutor()
    tool_obj = Tool(name="bash", handler=lambda cmd: "tiny", description="", parameters={})

    execution = await executor.execute_async([(tool_obj, {"cmd": "x"})], context=context)

    assert execution.tool_results == ["tiny"]


@pytest.mark.asyncio
async def test_tool_error_is_never_spilled(tmp_path: Path, spill_store: Any) -> None:
    def boom(cmd: str) -> str:
        raise ValueError("boom")

    executor = ToolExecutor()
    context = _make_context(tmp_path, spill_store)
    tool_obj = Tool(name="bash", handler=boom, description="", parameters={})

    execution = await executor.execute_async([(tool_obj, {"cmd": "x"})], context=context)

    assert execution.error is not None
    assert execution.error.details["error"] == "ValueError('boom')"
    assert not any(entry.kind == "tool_result" for entry in (spill_store._store.read(SPILL_TAPE) or []))


@pytest.mark.asyncio
async def test_spill_write_failure_keeps_original_result(tmp_path: Path) -> None:
    """A failing spill store degrades to the original result — never a failed turn."""

    class BrokenStore:
        async def append(self, tape: str, entry: Any) -> None:
            raise OSError("disk full")

        async def fetch_all(self, query: Any) -> Any:
            return []

    executor = ToolExecutor()
    context = _make_context(tmp_path, BrokenStore())
    tool_obj = Tool(name="bash", handler=lambda cmd: "x" * 100_000, description="", parameters={})

    execution = await executor.execute_async([(tool_obj, {"cmd": "x"})], context=context)

    assert execution.error is None
    assert execution.tool_results == ["x" * 100_000]


@pytest.mark.asyncio
async def test_spill_goes_to_spill_tape_through_forked_session_tape(tmp_path: Path, spill_store: Any) -> None:
    """Spill entries bypass the session-tape fork and land in the shared spill tape immediately."""
    parent = spill_store._store
    fork = ForkTapeStore(spill_store, "session-tape")
    executor = ToolExecutor()
    context = _make_context(tmp_path, fork)
    tool_obj = Tool(name="bash", handler=lambda cmd: "y" * 100_000, description="", parameters={})

    await executor.execute_async([(tool_obj, {"cmd": "x"})], context=context)

    entries = list(parent.read(SPILL_TAPE) or [])
    assert len(entries) == 1
    assert entries[0].payload["results"] == ["y" * 100_000]
    assert entries[0].meta["spill_handle"]


@pytest.mark.asyncio
async def test_read_spilled_unknown_handle_returns_none(spill_store: Any) -> None:
    assert await read_spilled(store=spill_store, handle="run-1/bash.deadbeef") is None


@pytest.mark.asyncio
async def test_read_tool_result_tool_is_bounded_and_literal(tmp_path: Path, spill_store: Any) -> None:
    """read_tool_result enforces bounds and treats pattern as a literal substring."""
    big = "\n".join(f"line-{i}" for i in range(3000))
    handle = handle_key("run-1", "bash")
    await spill_store.append(
        SPILL_TAPE, __import__("bub.tape", fromlist=["TapeEntry"]).TapeEntry.tool_result([big], spill_handle=handle)
    )

    context = _make_context(tmp_path, spill_store)
    result = await builtin_tools.read_tool_result.run(
        handle=handle, offset=0, limit=3, from_end=False, pattern=None, context=context
    )
    assert result.startswith("[handle: 3,000 matching line(s); showing 3]")
    assert "line-0" in result
    assert "line-2" in result

    tail = await builtin_tools.read_tool_result.run(
        handle=handle, offset=0, limit=3, from_end=True, pattern=None, context=context
    )
    assert "line-2999" in tail

    literal = await builtin_tools.read_tool_result.run(
        handle=handle, offset=0, limit=2000, from_end=False, pattern="line-1999", context=context
    )
    assert "line-1999" in literal
    assert "line-1998" not in literal  # literal substring, not a prefix match

    too_many = await builtin_tools.read_tool_result.run(
        handle=handle, offset=0, limit=MAX_READ_LINES + 100, from_end=False, pattern=None, context=context
    )
    assert "showing 1000" in too_many  # limit clamped


@pytest.mark.asyncio
async def test_read_tool_result_unknown_handle_is_friendly(tmp_path: Path, spill_store: Any) -> None:
    context = _make_context(tmp_path, spill_store)
    result = await builtin_tools.read_tool_result.run(
        handle="run-1/bash.nope", offset=0, limit=5, from_end=False, pattern=None, context=context
    )
    assert "No stored tool result" in result
    assert "re-run the original tool" in result


def test_read_slice_bounds_and_literal_pattern() -> None:
    output = "\n".join(f"line-{i}" for i in range(100))
    window = read_slice(output, offset=10, limit=5, from_end=False, pattern=None)
    assert "line-10" in window and "line-14" in window
    assert "line-9" not in window

    tail = read_slice(output, offset=0, limit=5, from_end=True, pattern=None)
    assert "line-99" in tail and "line-95" in tail

    filtered = read_slice(
        "\n".join(["foo", "bar-baz", "qux", "bar"]), offset=0, limit=10, from_end=False, pattern="bar"
    )
    assert filtered.count("bar") == 2  # literal substring matches every line containing it
    regexish = read_slice("\n".join(["bar", "b.r"]), offset=0, limit=10, from_end=False, pattern="b.r")
    assert regexish.count("b.r") == 1  # pattern is literal, not a regex


def test_spill_ref_is_self_describing() -> None:
    ref = spill_ref("run-1/bash.abc", "a\nb\nc\n")
    assert "handle: run-1/bash.abc" in ref
    assert "read_tool_result(handle=" in ref


@pytest.mark.asyncio
async def test_next_model_request_never_contains_full_payload(tmp_path: Path, spill_store: Any) -> None:
    """Regression: after a spill, the serialized request body sent to the model stays bounded."""
    big = "x" * 5_000_000  # multi-MB single-line output (the 413 scenario)
    executor = ToolExecutor()
    context = _make_context(tmp_path, spill_store)
    tool_obj = Tool(name="bash", handler=lambda cmd: big, description="", parameters={})

    execution = await executor.execute_async([(tool_obj, {"cmd": "grep -R foo"})], context=context)
    ref = execution.tool_results[0]
    assert "tool output spilled" in ref

    # What the next request would carry: the tool_result entry the model sees.
    messages = [{"role": "tool", "content": ref}]
    body = len(json.dumps(messages, ensure_ascii=False))
    assert body < 100_000  # orders of magnitude below any 413 threshold
    assert "x" * 1000 not in ref  # the raw payload is not inline


@pytest.mark.asyncio
async def test_hard_request_cap_clamps_oversized_messages() -> None:
    """Regression: even a bypassed spill (huge inline tool message) never sends an unbounded body."""
    runner = ModelRunner(AgentSettings.model_construct(model="openai:gpt-test", max_request_bytes=2048, max_tokens=100))
    messages = [{"role": "tool", "content": "z" * 100_000}]
    clamped = runner._clamp_oversized_messages(messages)
    body = json.dumps(clamped, ensure_ascii=False)
    assert len(body) < 4096
    assert "clamped" in clamped[0]["content"]
