from __future__ import annotations

import pytest

from bub.core.store import ForkTapeStore, InMemoryTapeStore, TapeQuery
from bub.core.tape import TapeEntry


@pytest.mark.asyncio
async def test_fork_merge_back_true_merges_entries() -> None:
    """With merge_back=True (default), forked entries are merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")

    await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))
    await store.append("test-tape", TapeEntry.event(name="step", data={"x": 2}))
    await store.merge_back()

    entries = await TapeQuery("test-tape", parent).all()
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_fork_buffers_managed_writes_until_merge() -> None:
    """Managed writes stay local until an explicit merge."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")

    await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))

    entries = await TapeQuery("test-tape", parent).all()
    # No entries should have been merged
    assert entries == []


@pytest.mark.asyncio
async def test_merge_back_can_be_called_without_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")

    await store.merge_back()

    entries = await TapeQuery("test-tape", parent).all()
    assert entries == []


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_false_preserves_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")
    (await parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1})))

    await store.reset("test-tape")
    await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))

    entries = await TapeQuery("test-tape", parent).all()
    assert [entry.payload["name"] for entry in entries] == ["before"]


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_true_replaces_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")
    (await parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1})))

    await store.reset("test-tape")
    await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))
    await store.merge_back()

    entries = await TapeQuery("test-tape", parent).all()
    assert [entry.payload["name"] for entry in entries] == ["inside"]


@pytest.mark.asyncio
async def test_fork_reset_hides_parent_entries_during_query() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")
    (await parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1})))

    await store.reset("test-tape")
    await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))

    query = TapeQuery(tape="test-tape", store=store)
    entries = await query.all()

    assert [entry.payload["name"] for entry in entries] == ["inside"]


@pytest.mark.asyncio
async def test_reset_for_unbound_tape_resets_parent_immediately() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "other-tape")
    (await parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1})))

    await store.reset("test-tape")

    entries = await TapeQuery("test-tape", parent).all()
    assert entries == []
