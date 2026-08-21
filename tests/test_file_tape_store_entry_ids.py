from __future__ import annotations

import pytest

from bub.sidecars import sidecar_tape_name
from bub.store import AsyncTapeStoreAdapter, FileTapeStore, ForkTapeStore
from bub.tape import TapeEntry


@pytest.mark.asyncio
async def test_file_tape_store_assigns_monotonic_ids_when_merging_forked_entries(tmp_path) -> None:
    parent = FileTapeStore(directory=tmp_path)
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "tape")

    await store.append("tape", TapeEntry.event(name="first", data={"n": 1}))
    await store.merge_back()

    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "tape")
    await store.append("tape", TapeEntry.event(name="second", data={"n": 2}))
    await store.merge_back()

    entries = parent.read("tape") or []
    assert [entry.id for entry in entries] == [1, 2]
    assert [entry.payload.get("name") for entry in entries] == ["first", "second"]


def test_file_tape_store_excludes_sidecar_tapes(tmp_path) -> None:
    store = FileTapeStore(directory=tmp_path)
    sidecar = sidecar_tape_name("session__id", "spill")
    store.append("session__id", TapeEntry.event(name="main"))
    store.append(sidecar, TapeEntry.event(name="spill"))

    assert store.list_tapes() == ["session__id"]
