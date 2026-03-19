from __future__ import annotations

import pytest
from republic import TapeEntry, TapeQuery
from republic.tape import InMemoryTapeStore

from bub.builtin.store import ForkTapeStore


@pytest.mark.asyncio
async def test_fork_merge_back_true_merges_entries() -> None:
    """With merge_back=True (default), forked entries are merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)

    async with store.fork("test-tape", merge_back=True):
        await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))
        await store.append("test-tape", TapeEntry.event(name="step", data={"x": 2}))

    entries = parent.read("test-tape")
    assert entries is not None
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_fork_merge_back_false_discards_entries() -> None:
    """With merge_back=False, forked entries are NOT merged into the parent."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)

    async with store.fork("test-tape", merge_back=False):
        await store.append("test-tape", TapeEntry.event(name="step", data={"x": 1}))

    entries = parent.read("test-tape")
    # No entries should have been merged
    assert entries is None or len(entries) == 0


@pytest.mark.asyncio
async def test_fork_default_merge_back_is_true() -> None:
    """The default value of merge_back should be True."""
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)

    async with store.fork("test-tape"):
        await store.append("test-tape", TapeEntry.event(name="step", data={"v": 1}))

    entries = parent.read("test-tape")
    assert entries is not None
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_false_preserves_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    async with store.fork("test-tape", merge_back=False):
        await store.reset("test-tape")
        await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))

    entries = parent.read("test-tape")
    assert entries is not None
    assert [entry.payload["name"] for entry in entries] == ["before"]


@pytest.mark.asyncio
async def test_fork_reset_with_merge_back_true_replaces_parent_entries() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    async with store.fork("test-tape", merge_back=True):
        await store.reset("test-tape")
        await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))

    entries = parent.read("test-tape")
    assert entries is not None
    assert [entry.payload["name"] for entry in entries] == ["inside"]


@pytest.mark.asyncio
async def test_fork_reset_hides_parent_entries_during_fetch() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    async with store.fork("test-tape", merge_back=False):
        await store.reset("test-tape")
        await store.append("test-tape", TapeEntry.event(name="inside", data={"x": 2}))

        query = TapeQuery(tape="test-tape", store=store)
        entries = list(await store.fetch_all(query))

    assert [entry.payload["name"] for entry in entries] == ["inside"]


@pytest.mark.asyncio
async def test_reset_outside_fork_resets_parent_immediately() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(parent)
    parent.append("test-tape", TapeEntry.event(name="before", data={"x": 1}))

    await store.reset("test-tape")

    entries = parent.read("test-tape")
    assert entries is None
