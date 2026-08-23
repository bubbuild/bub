from __future__ import annotations

from pathlib import Path

import pytest

from bub.builtin.forkmerge import ForkMergeSidecar
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
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
async def test_tape_fork_is_isolated_until_explicit_merge(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    forkmerge = ForkMergeSidecar()
    root = Tape(tmp_path, AsyncTapeStoreAdapter(parent), TapeContext(), sidecars=(forkmerge,)).scoped("test-tape")

    tape_fork = await forkmerge.fork(root)

    await tape_fork.tape.append_event("step", {"value": 1})
    assert parent.read("test-tape") is None

    await tape_fork.merge()

    assert [entry.payload["name"] for entry in parent.read("test-tape") or []] == ["step"]


@pytest.mark.asyncio
async def test_discarded_session_does_not_change_parent(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    forkmerge = ForkMergeSidecar()
    root = Tape(tmp_path, AsyncTapeStoreAdapter(parent), TapeContext(), sidecars=(forkmerge,)).scoped("test-tape")
    tape_fork = await forkmerge.fork(root)

    await tape_fork.tape.append_event("step", {"value": 2})
    await tape_fork.discard()

    assert parent.read("test-tape") is None


@pytest.mark.asyncio
async def test_discarding_a_reset_fork_preserves_parent_entries(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    parent.append("test-tape", TapeEntry.event(name="before"))
    forkmerge = ForkMergeSidecar()
    root = Tape(tmp_path, AsyncTapeStoreAdapter(parent), TapeContext(), sidecars=(forkmerge,)).scoped("test-tape")
    tape_fork = await forkmerge.fork(root)

    await tape_fork.tape.reset()
    await tape_fork.tape.append_event("inside", {})
    await tape_fork.discard()

    assert [entry.payload["name"] for entry in parent.read("test-tape") or []] == ["before"]


@pytest.mark.asyncio
async def test_merging_a_reset_fork_replaces_parent_entries(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    parent.append("test-tape", TapeEntry.event(name="before"))
    forkmerge = ForkMergeSidecar()
    root = Tape(tmp_path, AsyncTapeStoreAdapter(parent), TapeContext(), sidecars=(forkmerge,)).scoped("test-tape")
    tape_fork = await forkmerge.fork(root)

    await tape_fork.tape.reset()
    await tape_fork.tape.append_event("inside", {})
    assert [entry.payload["name"] for entry in await tape_fork.tape.store.fetch_all(tape_fork.tape.query())] == [
        "session/start",
        "handoff",
        "inside",
    ]
    await tape_fork.merge()

    assert [entry.payload["name"] for entry in parent.read("test-tape") or []] == [
        "session/start",
        "handoff",
        "inside",
    ]


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
