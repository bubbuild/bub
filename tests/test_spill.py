from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bub.builtin.context import default_tape_context
from bub.builtin.hook_impl import BuiltinImpl
from bub.builtin.spill import (
    SPILL_READ_MODEL_NAME,
    SPILL_READ_TOOL_NAME,
    SpillSettings,
    SpillStore,
    spill_read,
    spill_tape_name,
)
from bub.builtin.tools import render_tools_prompt
from bub.hooks.interception import ToolCall, ToolCallDecision, ToolCallResult
from bub.store import AsyncTapeStoreAdapter, FileTapeStore, InMemoryTapeStore, TapeStore
from bub.tape import Tape
from bub.tools import Tool, ToolContext, ToolExecutor, model_tools


class _SpillHooks:
    async def before_tool_call(self, call: ToolCall, state: dict[str, Any]) -> tuple[ToolCall, ToolCallDecision]:
        return call, ToolCallDecision.proceed()

    async def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict[str, Any]) -> None:
        await BuiltinImpl.after_tool_call(self, call, result, state)  # type: ignore[arg-type]


def _spill_executor() -> ToolExecutor:
    return ToolExecutor(hooks=_SpillHooks())  # type: ignore[arg-type]


def _handle_from_ref(ref: str) -> str:
    return ref.split("handle: ", 1)[1].split("]", 1)[0]


def _page_content(page: str) -> str:
    return page.split("content:\n", 1)[1]


def _page_field(page: str, name: str) -> str:
    prefix = f"{name}: "
    return next(line.removeprefix(prefix) for line in page.splitlines() if line.startswith(prefix))


def _root_tape(tmp_path: Path, store: TapeStore, *, threshold: int = 1) -> Tape:
    spill = SpillStore(SpillSettings(threshold=threshold))
    return Tape(tmp_path, AsyncTapeStoreAdapter(store), default_tape_context(), sidecars=(spill,)).scoped("session")


async def _read_page(
    context: ToolContext,
    handle: str,
    *,
    cursor: int = 0,
    count: int = 1,
    from_end: bool = False,
) -> str:
    execution = await _spill_executor().execute_async(
        [(spill_read, {"handle": handle, "cursor": cursor, "count": count, "from_end": from_end})],
        context=context,
    )
    page = execution.tool_results[0]
    assert isinstance(page, str)
    return page


@pytest.mark.asyncio
async def test_oversized_result_is_bounded_and_readable_across_merge(tmp_path: Path) -> None:
    parent = FileTapeStore(tmp_path / "tapes")
    root = _root_tape(tmp_path, parent)
    output = ("alpha🙂beta\n" * 5000) + "the-end"

    async with root.fork_tape() as tape:
        context = ToolContext(tape=tape, run_id="run-1")
        tool = Tool(name="large", handler=lambda: output)
        execution = await _spill_executor().execute_async([(tool, {})], context=context)

        ref = execution.tool_results[0]
        assert isinstance(ref, str)
        assert "tool output spilled" in ref
        assert len(ref) < 2000
        handle = _handle_from_ref(ref)

        cursor = 0
        restored: list[str] = []
        while True:
            page = await _read_page(context, handle, cursor=cursor, count=2)
            restored.append(_page_content(page))
            if _page_field(page, "complete") == "true":
                break
            cursor = int(_page_field(page, "next_cursor"))

        assert "".join(restored) == output

        tail = await _read_page(context, handle, from_end=True)
        assert _page_content(tail).endswith("the-end")

        await tape.record_chat(
            run_id="run-1",
            system_prompt=None,
            new_messages=[],
            response_text=None,
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "large", "arguments": "{}"}}],
            tool_results=execution.tool_results,
        )
        request_messages = await tape.read_messages()
        request_body = json.dumps(request_messages, ensure_ascii=False)
        assert handle in request_body
        assert output not in request_body

    persisted_context = ToolContext(tape=root, run_id="run-2")
    persisted = await _read_page(persisted_context, handle)
    assert _page_content(persisted) == restored[0][: len(_page_content(persisted))]


@pytest.mark.asyncio
async def test_spill_configuration_preserves_results_that_should_not_be_spilled(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent, threshold=100)

    async with root.fork_tape() as tape:
        context = ToolContext(tape=tape, run_id="run-1")
        small = await _spill_executor().execute_async(
            [(Tool(name="small", handler=lambda: "tiny"), {})], context=context
        )

        assert small.tool_results == ["tiny"]

    disabled = _root_tape(tmp_path, parent, threshold=0).scoped("disabled")
    async with disabled.fork_tape() as tape:
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: "x" * 20_000), {})],
            context=ToolContext(tape=tape, run_id="run-2"),
        )
    assert execution.tool_results == ["x" * 20_000]


@pytest.mark.asyncio
async def test_temporary_fork_discards_spilled_content(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent)

    async with root.fork_tape(merge_back=False) as tape:
        context = ToolContext(tape=tape, run_id="run-1")
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: "x" * 20_000), {})], context=context
        )
        handle = _handle_from_ref(execution.tool_results[0])
        assert "content:" in await _read_page(context, handle)

    missing = await _read_page(ToolContext(tape=root), handle)
    assert "no spilled tool result" in missing


@pytest.mark.asyncio
async def test_unknown_handle_and_invalid_read_bounds_are_friendly(tmp_path: Path) -> None:
    root = _root_tape(tmp_path, InMemoryTapeStore())
    context = ToolContext(tape=root)

    assert "no spilled tool result" in await spill_read.run(handle="missing", context=context)
    assert await spill_read.run(handle="missing", cursor=-1, context=context) == "`cursor` must be >= 0."
    assert await spill_read.run(handle="missing", count=0, context=context) == "`count` must be >= 1."


def test_spill_read_uses_the_builtin_tool_naming_convention() -> None:
    assert spill_read.name == SPILL_READ_TOOL_NAME == "spill.read"
    assert model_tools([spill_read])[0].name == SPILL_READ_MODEL_NAME == "spill_read"
    assert "spill_read(handle, cursor?, count?, from_end?)" in render_tools_prompt([spill_read])


@pytest.mark.asyncio
async def test_tape_archive_preserves_spilled_results_and_clears_the_session(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent)
    sidecar = spill_tape_name(root.name)
    await root.ensure_bootstrap_anchor()

    async with root.fork_tape() as tape:
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: "archived output\n" * 5000), {})],
            context=ToolContext(tape=tape, run_id="run-1"),
        )
        ref = execution.tool_results[0]
        assert isinstance(ref, str)
        handle = _handle_from_ref(ref)
        await tape.record_chat(
            run_id="run-1",
            system_prompt=None,
            new_messages=[{"role": "user", "content": "archive this"}],
            response_text=None,
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "large", "arguments": "{}"}}],
            tool_results=execution.tool_results,
        )

    result = await root.reset(archive=True)

    main_archive = Path(result.removeprefix("Archived: "))
    spill_archives = list(tmp_path.glob(f"{sidecar}.jsonl.*.bak"))
    assert main_archive.exists()
    assert len(spill_archives) == 1
    assert handle in main_archive.read_text(encoding="utf-8")
    assert handle in spill_archives[0].read_text(encoding="utf-8")
    assert "no spilled tool result" in await _read_page(ToolContext(tape=root), handle)
