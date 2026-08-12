from __future__ import annotations

import pytest

from bub.store import AsyncTapeStoreAdapter, ForkTapeStore, InMemoryTapeStore, TapeQuery
from bub.tape import TapeEntry


@pytest.mark.asyncio
async def test_fork_merge_back_true_merges_entries() -> None:
    """With merge_back=True (default), forked entries are merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "test-tape")

    await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))
    await store.append("test-tape", TapeEntry.event(name="step", data={"x": 2}))
    await store.merge_back()

    entries = parent.read("test-tape")
    assert entries is not None
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_fork_merge_back_false_discards_entries() -> None:
    """With merge_back=False, forked entries are NOT merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "test-tape")

    await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))

    entries = parent.read("test-tape")
    # No entries should have been merged
    assert entries is None or len(entries) == 0


@pytest.mark.asyncio
async def test_merge_back_can_be_called_without_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "test-tape")

    await store.merge_back()

    entries = parent.read("test-tape")
    assert entries is None or len(entries) == 0


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_false_preserves_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "test-tape")
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    await store.reset("test-tape")
    await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))

    entries = parent.read("test-tape")
    assert entries is not None
    assert [entry.payload["name"] for entry in entries] == ["before"]


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_true_replaces_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "test-tape")
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    await store.reset("test-tape")
    await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))
    await store.merge_back()

    entries = parent.read("test-tape")
    assert entries is not None
    assert [entry.payload["name"] for entry in entries] == ["inside"]


@pytest.mark.asyncio
async def test_fork_reset_hides_parent_entries_during_fetch() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "test-tape")
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    await store.reset("test-tape")
    await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))

    query = TapeQuery(tape="test-tape", store=store)
    entries = list(await store.fetch_all(query))

    assert [entry.payload["name"] for entry in entries] == ["inside"]


@pytest.mark.asyncio
async def test_reset_for_unbound_tape_resets_parent_immediately() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "other-tape")
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    await store.reset("test-tape")

    entries = parent.read("test-tape")
    assert entries is None


@pytest.mark.asyncio
async def test_sidecar_merge_failure_records_event_and_still_merges_main_tape() -> None:
    class BrokenSidecarStore(InMemoryTapeStore):
        def append(self, tape: str, entry: TapeEntry) -> None:
            if tape == "session__spill":
                raise OSError("sidecar unavailable")
            super().append(tape, entry)

    parent = BrokenSidecarStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "session", sidecars=("session__spill",))
    await store.append("session__spill", TapeEntry.tool_result(["full output"]))
    await store.append("session", TapeEntry.tool_result(["ref"]))

    await store.merge_back()

    entries = parent.read("session") or []
    assert any(entry.kind == "tool_result" and entry.payload["results"] == ["ref"] for entry in entries)
    merge_event = next(entry for entry in entries if entry.payload.get("name") == "sidecar.merge")
    assert merge_event.payload["data"] == {
        "tape": "session__spill",
        "status": "error",
        "error": "sidecar unavailable",
    }
