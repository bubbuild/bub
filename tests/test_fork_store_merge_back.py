from __future__ import annotations

import pytest

from bub.store import ForkTapeStore, InMemoryTapeStore, TapeQuery
from bub.tape import bub_event, bub_event_type


@pytest.mark.asyncio
async def test_fork_merge_back_true_merges_entries() -> None:
    """With merge_back=True (default), forked entries are merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")

    first = await store.append("test-tape", bub_event(name="step", data={"x": 1}))
    await store.append("test-tape", bub_event(name="step", data={"x": 2}))
    await store.merge_back()

    records = [record async for record in parent.scan(TapeQuery("test-tape"))]
    assert records is not None
    assert len(records) == 2
    assert records[0].event.get_id() == first.event.get_id()
    assert records[0].cursor == 1


@pytest.mark.asyncio
async def test_fork_cursors_continue_after_the_parent_snapshot() -> None:
    parent = InMemoryTapeStore()
    await parent.append("test-tape", bub_event(name="before"))
    store = ForkTapeStore(parent, "test-tape")

    appended = await store.append("test-tape", bub_event(name="inside"))
    entries = [entry async for entry in store.scan(TapeQuery("test-tape"))]

    assert appended.cursor == 2
    assert [entry.cursor for entry in entries] == [1, 2]


@pytest.mark.asyncio
async def test_fork_merge_back_false_discards_entries() -> None:
    """With merge_back=False, forked entries are NOT merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")

    await store.append("test-tape", bub_event(name="step", data={"x": 1}))

    entries = [entry async for entry in parent.scan(TapeQuery("test-tape"))]
    # No entries should have been merged
    assert entries is None or len(entries) == 0


@pytest.mark.asyncio
async def test_merge_back_can_be_called_without_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")

    await store.merge_back()

    entries = [entry async for entry in parent.scan(TapeQuery("test-tape"))]
    assert entries is None or len(entries) == 0


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_false_preserves_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")
    await parent.append("test-tape", bub_event(name="before", data={"x": 1}))

    await store.reset("test-tape")
    await store.append("test-tape", bub_event(name="inside", data={"x": 2}))

    entries = [entry async for entry in parent.scan(TapeQuery("test-tape"))]
    assert entries is not None
    assert [entry.event.get_type() for entry in entries] == [bub_event_type("before")]


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_true_replaces_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")
    await parent.append("test-tape", bub_event(name="before", data={"x": 1}))

    await store.reset("test-tape")
    await store.append("test-tape", bub_event(name="inside", data={"x": 2}))
    await store.merge_back()

    entries = [entry async for entry in parent.scan(TapeQuery("test-tape"))]
    assert entries is not None
    assert [entry.event.get_type() for entry in entries] == [bub_event_type("inside")]


@pytest.mark.asyncio
async def test_fork_reset_hides_parent_entries_during_fetch() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "test-tape")
    await parent.append("test-tape", bub_event(name="before", data={"x": 1}))

    await store.reset("test-tape")
    await store.append("test-tape", bub_event(name="inside", data={"x": 2}))

    query = TapeQuery(tape="test-tape")
    entries = [entry async for entry in store.scan(query)]

    assert [entry.event.get_type() for entry in entries] == [bub_event_type("inside")]


@pytest.mark.asyncio
async def test_reset_for_unbound_tape_resets_parent_immediately() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent, "other-tape")
    await parent.append("test-tape", bub_event(name="before", data={"x": 1}))

    await store.reset("test-tape")

    entries = [entry async for entry in parent.scan(TapeQuery("test-tape"))]
    assert entries == []
