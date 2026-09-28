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
async def test_media_replay_fields_survive_merge_while_unknown_blocks_stay_filtered() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "test-tape")
    media = [
        {"type": "text", "text": "compare"},
        {"type": "image_url", "image_url": {"url": "https://assets.test/image", "detail": "low"}},
        {"type": "input_audio", "input_audio": {"data": "YXVkaW8=", "format": "wav"}},
        {"type": "video_url", "video_url": {"url": "https://assets.test/video"}, "processing": "static"},
    ]
    excluded = [
        {"type": "private", "value": "not a model media part"},
        {"type": "image_url", "image_url": {"url": "https://assets.test/image", "private": "extra"}},
        {"type": "input_audio", "input_audio": {"data": "YQ==", "format": "wav"}, "private": "extra"},
    ]
    await store.append("test-tape", TapeEntry.message({"role": "user", "content": [*media, *excluded]}))
    await store.merge_back()
    entries = parent.read("test-tape")
    assert entries is not None and entries[0].payload["content"] == media
