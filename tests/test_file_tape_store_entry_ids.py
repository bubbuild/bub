from __future__ import annotations

import pytest

from bub.builtin.store import FileTapeStore, ForkTapeStore
from bub.tape import AsyncTapeStoreAdapter, TapeEntry


def stream_entry(value: int) -> TapeEntry:
    return TapeEntry(id=0, kind="record", payload={"value": value})


@pytest.mark.asyncio
async def test_file_tape_store_assigns_monotonic_ids_when_merging_forked_entries(tmp_path) -> None:
    parent = FileTapeStore(directory=tmp_path)
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "tape")

    await store.append("tape", stream_entry(1))
    await store.merge_back()

    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "tape")
    await store.append("tape", stream_entry(2))
    await store.merge_back()

    entries = parent.read("tape") or []
    assert [entry.id for entry in entries] == [1, 2]
    assert [entry.payload["value"] for entry in entries] == [1, 2]
