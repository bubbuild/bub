from __future__ import annotations

import pytest
from republic import TapeEntry

from bub.builtin.store import FileTapeStore, ForkTapeStore


@pytest.mark.asyncio
async def test_file_tape_store_assigns_monotonic_ids_when_merging_forked_entries(tmp_path) -> None:
    parent = FileTapeStore(directory=tmp_path)
    store = ForkTapeStore(parent)

    async with store.fork("tape", merge_back=True):
        await store.append("tape", TapeEntry.event(name="first", data={"n": 1}))

    async with store.fork("tape", merge_back=True):
        await store.append("tape", TapeEntry.event(name="second", data={"n": 2}))

    entries = parent.read("tape") or []
    assert [entry.id for entry in entries] == [1, 2]
    assert [entry.payload.get("name") for entry in entries] == ["first", "second"]
