from __future__ import annotations

from pathlib import Path

import pytest

from bub.builtin.context import default_tape_context
from bub.store import AsyncTapeStoreAdapter, ForkTapeStore, InMemoryTapeStore
from bub.tape import Tape, TapeContext, TapeEntry


def test_tape_reexports_legacy_store_objects() -> None:
    from bub import store, tape

    expected_exports = {
        "AsyncTapeStore",
        "AsyncTapeStoreAdapter",
        "InMemoryQueryMixin",
        "InMemoryTapeStore",
        "TapeQuery",
        "TapeStore",
        "UnavailableTapeStore",
        "is_async_tape_store",
    }

    assert expected_exports <= set(dir(tape))
    for name in expected_exports:
        assert getattr(tape, name) is getattr(store, name)


@pytest.mark.asyncio
async def test_legacy_tool_call_without_content_replays_with_its_result(tmp_path: Path) -> None:
    store = InMemoryTapeStore()
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(store), default_tape_context()).scoped("test-tape")
    await tape.ensure_bootstrap_anchor()
    calls = [{"id": "call-1", "type": "function", "function": {"name": "inspect", "arguments": "{}"}}]
    store.append("test-tape", TapeEntry(id=0, kind="tool_call", payload={"calls": calls}))
    store.append("test-tape", TapeEntry.tool_result(["files found"]))

    assert await tape.read_messages() == [
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "content": "files found", "tool_call_id": "call-1", "name": "inspect"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "done"])
async def test_text_only_response_remains_a_standalone_assistant_message(tmp_path: Path, content: str) -> None:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), default_tape_context()).scoped("test-tape")
    await tape.ensure_bootstrap_anchor()
    await tape.record_chat(run_id="run-1", system_prompt=None, new_messages=[], response_text=content)

    assert await tape.read_messages() == [{"role": "assistant", "content": content}]


@pytest.mark.asyncio
async def test_tape_fork_binds_temporary_fork_store_to_scoped_tape(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = Tape(tmp_path, AsyncTapeStoreAdapter(parent), TapeContext()).scoped("test-tape")

    async with root.fork_tape(merge_back=True) as forked:
        first_store = forked.store

        assert isinstance(first_store, ForkTapeStore)
        assert first_store is not root.store

        await forked.append_event("step", {"value": 1})
        assert parent.read("test-tape") is None

    assert [entry.payload["name"] for entry in parent.read("test-tape") or []] == ["step"]

    async with root.fork_tape(merge_back=False) as forked:
        second_store = forked.store
        await forked.append_event("step", {"value": 2})

    assert isinstance(second_store, ForkTapeStore)
    assert second_store is not first_store
    assert [entry.payload["data"]["value"] for entry in parent.read("test-tape") or []] == [1]


@pytest.mark.asyncio
async def test_tape_info_reports_last_token_cache_hit_rate(tmp_path: Path) -> None:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    await tape.record_chat(
        run_id="run-1",
        system_prompt=None,
        new_messages=[],
        response_text=None,
        usage={
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 60},
        },
    )

    info = await tape.info()

    assert info.last_token_usage == 100
    assert info.last_token_cache_hit_rate == 0.75


@pytest.mark.asyncio
async def test_tape_info_omits_cache_hit_rate_when_usage_has_no_cache_details(tmp_path: Path) -> None:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    await tape.record_chat(
        run_id="run-1",
        system_prompt=None,
        new_messages=[],
        response_text=None,
        usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
    )

    info = await tape.info()

    assert info.last_token_cache_hit_rate is None


@pytest.mark.asyncio
async def test_context_excluded_entries_do_not_reach_custom_context_selectors(tmp_path: Path) -> None:
    def select_events(entries, _context):
        return [
            {"role": "assistant", "content": str(entry.payload.get("name"))}
            for entry in entries
            if entry.kind == "event"
        ]

    tape = Tape(
        tmp_path,
        AsyncTapeStoreAdapter(InMemoryTapeStore()),
        TapeContext(anchor=None, select=select_events),
    ).scoped("test-tape")
    await tape.append_event("visible", {})
    await tape.append_event("hidden", {}, context=False)

    assert await tape.read_messages() == [{"role": "assistant", "content": "visible"}]
