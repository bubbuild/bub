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
from bub.store import AsyncTapeStoreAdapter, FileTapeStore, InMemoryTapeStore
from bub.tape import Tape, TapeContext, TapeEntry
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


def _root_tape(tmp_path: Path, store: InMemoryTapeStore, *, threshold: int = 1) -> Tape:
    spill = SpillStore(SpillSettings(threshold=threshold))
    return Tape(tmp_path, AsyncTapeStoreAdapter(store), default_tape_context(), sidecars=(spill,)).scoped("session")


@pytest.mark.asyncio
async def test_oversized_result_is_bounded_and_readable_across_merge(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent)
    output = ("alpha🙂beta\n" * 5000) + "the-end"
    sidecar = spill_tape_name(root.name)

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
            page = await spill_read.run(handle=handle, cursor=cursor, count=2, context=context)
            restored.append(_page_content(page))
            if _page_field(page, "complete") == "true":
                break
            cursor = int(_page_field(page, "next_cursor"))

        assert "".join(restored) == output

        tail = await spill_read.run(handle=handle, cursor=0, count=1, from_end=True, context=context)
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

        assert parent.read(sidecar) is None

    persisted_context = ToolContext(tape=root, run_id="run-2")
    persisted = await spill_read.run(handle=handle, cursor=0, count=1, context=persisted_context)
    assert _page_content(persisted) == restored[0][: len(_page_content(persisted))]
    assert parent.read(sidecar)
    write_events = [
        entry
        for entry in parent.read(root.name) or []
        if entry.kind == "event" and entry.payload.get("name") == "spill.write"
    ]
    assert len(write_events) == 1
    assert write_events[0].payload["data"]["status"] == "ok"
    assert write_events[0].payload["data"]["handle"] == handle


@pytest.mark.asyncio
async def test_small_results_and_errors_are_not_spilled(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent, threshold=100)
    sidecar = spill_tape_name(root.name)

    def fail() -> str:
        raise ValueError("boom")

    async with root.fork_tape() as tape:
        context = ToolContext(tape=tape, run_id="run-1")
        small = await _spill_executor().execute_async(
            [(Tool(name="small", handler=lambda: "tiny"), {})], context=context
        )
        spill_page = await _spill_executor().execute_async(
            [(Tool(name=SPILL_READ_MODEL_NAME, handler=lambda: "x" * 20_000), {})], context=context
        )
        failed = await _spill_executor().execute_async([(Tool(name="failed", handler=fail), {})], context=context)

        assert small.tool_results == ["tiny"]
        assert spill_page.tool_results == ["x" * 20_000]
        assert failed.error is not None

    assert parent.read(sidecar) is None

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
    sidecar = spill_tape_name(root.name)

    async with root.fork_tape(merge_back=False) as tape:
        context = ToolContext(tape=tape, run_id="run-1")
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: "x" * 20_000), {})], context=context
        )
        handle = _handle_from_ref(execution.tool_results[0])
        assert "content:" in await spill_read.run(handle=handle, context=context)

    assert parent.read(sidecar) is None
    missing = await spill_read.run(handle=handle, context=ToolContext(tape=root))
    assert "no spilled tool result" in missing


@pytest.mark.asyncio
async def test_spill_failure_degrades_to_a_bounded_result(tmp_path: Path) -> None:
    class BrokenStore:
        async def list_tapes(self) -> list[str]:
            return []

        async def reset(self, tape: str) -> None:
            pass

        async def fetch_all(self, query: Any) -> list[Any]:
            return []

        async def append(self, tape: str, entry: Any) -> None:
            raise OSError("disk full")

    spill = SpillStore(SpillSettings(threshold=1))
    tape = Tape(tmp_path, BrokenStore(), TapeContext(), sidecars=(spill,)).scoped("session")
    context = ToolContext(tape=tape, run_id="run-1")
    output = "x" * 100_000

    execution = await _spill_executor().execute_async(
        [(Tool(name="large", handler=lambda: output), {})], context=context
    )

    result = execution.tool_results[0]
    assert execution.error is None
    assert isinstance(result, str)
    assert "spill storage failed" in result
    assert len(result) < 2000


@pytest.mark.asyncio
async def test_unknown_handle_and_invalid_read_bounds_are_friendly(tmp_path: Path) -> None:
    root = _root_tape(tmp_path, InMemoryTapeStore())
    context = ToolContext(tape=root)

    assert "no spilled tool result" in await spill_read.run(handle="missing", context=context)
    assert await spill_read.run(handle="missing", cursor=-1, context=context) == "`cursor` must be >= 0."
    assert await spill_read.run(handle="missing", count=0, context=context) == "`count` must be >= 1."


@pytest.mark.asyncio
async def test_spill_uses_the_regular_tape_store_contract(tmp_path: Path) -> None:
    store = FileTapeStore(tmp_path / "tapes")
    spill = SpillStore(SpillSettings(threshold=1))
    root = Tape(tmp_path, AsyncTapeStoreAdapter(store), default_tape_context(), sidecars=(spill,)).scoped("session")
    output = "stored through the native tape store\n" * 1000

    async with root.fork_tape() as tape:
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: output), {})],
            context=ToolContext(tape=tape, run_id="run-1"),
        )
        handle = _handle_from_ref(execution.tool_results[0])

    context = ToolContext(tape=root, run_id="run-2")
    first_page = await spill_read.run(handle=handle, count=1, context=context)

    assert output.startswith(_page_content(first_page))


def test_spill_read_uses_the_builtin_tool_naming_convention() -> None:
    assert spill_read.name == SPILL_READ_TOOL_NAME == "spill.read"
    assert model_tools([spill_read])[0].name == SPILL_READ_MODEL_NAME == "spill_read"
    assert "spill_read(handle, cursor?, count?, from_end?)" in render_tools_prompt([spill_read])


@pytest.mark.asyncio
async def test_spilled_result_keeps_the_recorded_model_prefix_stable(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent)
    output = "cache-prefix\n" * 5000
    await root.ensure_bootstrap_anchor()

    async with root.fork_tape() as tape:
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: output), {})],
            context=ToolContext(tape=tape, run_id="run-1"),
        )
        ref = execution.tool_results[0]
        assert isinstance(ref, str)
        assert f"[read with: {SPILL_READ_MODEL_NAME}(" in ref
        await tape.record_chat(
            run_id="run-1",
            system_prompt=None,
            new_messages=[{"role": "user", "content": "produce a large result"}],
            response_text=None,
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "large", "arguments": "{}"}}],
            tool_results=execution.tool_results,
        )

    cached_prefix = await root.read_messages()
    serialized_prefix = json.dumps(cached_prefix, ensure_ascii=False, separators=(",", ":"))
    assert serialized_prefix == json.dumps(await root.read_messages(), ensure_ascii=False, separators=(",", ":"))
    assert output not in serialized_prefix

    await root.record_chat(
        run_id="run-2",
        system_prompt=None,
        new_messages=[{"role": "user", "content": "continue"}],
        response_text="done",
    )

    extended_messages = await root.read_messages()
    assert extended_messages[: len(cached_prefix)] == cached_prefix


@pytest.mark.asyncio
async def test_tape_reset_clears_the_spill_sidecar_with_the_main_tape(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent)
    sidecar = spill_tape_name(root.name)
    await root.ensure_bootstrap_anchor()

    async with root.fork_tape() as tape:
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: "old output\n" * 5000), {})],
            context=ToolContext(tape=tape, run_id="run-1"),
        )
        ref = execution.tool_results[0]
        assert isinstance(ref, str)
        handle = _handle_from_ref(ref)
        await tape.record_chat(
            run_id="run-1",
            system_prompt=None,
            new_messages=[{"role": "user", "content": "produce output"}],
            response_text=None,
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "large", "arguments": "{}"}}],
            tool_results=execution.tool_results,
        )

    assert parent.read(sidecar)

    async with root.fork_tape() as tape:
        await tape.reset()
        missing = await spill_read.run(handle=handle, context=ToolContext(tape=tape))
        assert "no spilled tool result" in missing
        assert parent.read(sidecar)

    assert parent.read(sidecar) is None
    assert "no spilled tool result" in await spill_read.run(handle=handle, context=ToolContext(tape=root))
    assert [entry.payload.get("name") for entry in parent.read(root.name) or [] if entry.kind == "anchor"] == [
        "session/start"
    ]


@pytest.mark.asyncio
async def test_tape_archive_preserves_main_and_spill_as_sibling_tapes(tmp_path: Path) -> None:
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
    assert parent.read(sidecar) is None
    assert "no spilled tool result" in await spill_read.run(handle=handle, context=ToolContext(tape=root))


@pytest.mark.asyncio
async def test_spill_sidecar_can_be_archived_and_reset_without_changing_main_context(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = _root_tape(tmp_path, parent)
    sidecar = spill_tape_name(root.name)
    await root.ensure_bootstrap_anchor()

    async with root.fork_tape() as tape:
        execution = await _spill_executor().execute_async(
            [(Tool(name="large", handler=lambda: "gc output\n" * 5000), {})],
            context=ToolContext(tape=tape, run_id="run-1"),
        )
        ref = execution.tool_results[0]
        assert isinstance(ref, str)
        handle = _handle_from_ref(ref)
        await tape.record_chat(
            run_id="run-1",
            system_prompt=None,
            new_messages=[{"role": "user", "content": "retain the main tape"}],
            response_text=None,
            tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "large", "arguments": "{}"}}],
            tool_results=execution.tool_results,
        )

    messages_before = await root.read_messages()
    archive_result = await root.archive_sidecar("spill", reason="gc")

    assert archive_result.startswith("Archived spill: ")
    assert parent.read(sidecar)
    assert await root.read_messages() == messages_before

    reset_result = await root.reset_sidecar("spill", reason="gc")

    assert reset_result == "ok"
    assert parent.read(sidecar) is None
    assert await root.read_messages() == messages_before
    assert "no spilled tool result" in await spill_read.run(handle=handle, context=ToolContext(tape=root))
    lifecycle_events = [
        entry
        for entry in parent.read(root.name) or []
        if entry.kind == "event" and entry.payload.get("name") in {"sidecar.archive", "sidecar.reset"}
    ]
    assert [
        (entry.payload["name"], entry.payload["data"]["sidecar"], entry.payload["data"]["status"])
        for entry in lifecycle_events
    ] == [
        ("sidecar.archive", "spill", "ok"),
        ("sidecar.reset", "spill", "ok"),
    ]


@pytest.mark.asyncio
async def test_failed_spill_archive_preserves_sidecar_without_blocking_main_reset(tmp_path: Path) -> None:
    class BrokenSidecarArchiveStore(InMemoryTapeStore):
        def fetch_all(self, query: Any) -> Any:
            if query.tape.endswith("__spill"):
                raise OSError("spill archive unavailable")
            return super().fetch_all(query)

    parent = BrokenSidecarArchiveStore()
    root = _root_tape(tmp_path, parent)
    await root.ensure_bootstrap_anchor()
    sidecar = spill_tape_name(root.name)
    parent.append(sidecar, TapeEntry.event("spill.manifest"))

    result = await root.reset(archive=True)

    assert result.startswith("Archived: ")
    assert parent.read(sidecar)
    assert [entry.payload.get("name") for entry in parent.read(root.name) or [] if entry.kind == "anchor"] == [
        "session/start"
    ]
    lifecycle_events = [
        entry
        for entry in parent.read(root.name) or []
        if entry.kind == "event" and entry.payload.get("name") in {"sidecar.archive", "sidecar.reset"}
    ]
    assert [
        (entry.payload["name"], entry.payload["data"]["sidecar"], entry.payload["data"]["status"])
        for entry in lifecycle_events
    ] == [
        ("sidecar.archive", "spill", "error"),
        ("sidecar.reset", "spill", "skipped"),
    ]
