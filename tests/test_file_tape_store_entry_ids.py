from __future__ import annotations

import pytest

from bub.builtin.forkmerge import ForkMergeSidecar
from bub.sidecars import sidecar_tape_name
from bub.store import AsyncTapeStoreAdapter, FileTapeStore
from bub.tape import Tape, TapeContext, TapeEntry


@pytest.mark.asyncio
async def test_file_tape_store_assigns_monotonic_ids_when_merging_forked_entries(tmp_path) -> None:
    parent = FileTapeStore(directory=tmp_path)
    forkmerge = ForkMergeSidecar()
    tape = Tape(
        tmp_path,
        AsyncTapeStoreAdapter(parent),
        TapeContext(),
        sidecars=(forkmerge,),
    ).scoped("tape")

    first_fork = await forkmerge.fork(tape)
    await first_fork.tape.append_event("first", {"n": 1})
    await first_fork.merge()

    second_fork = await forkmerge.fork(tape)
    await second_fork.tape.append_event("second", {"n": 2})
    await second_fork.merge()

    entries = parent.read("tape") or []
    assert [entry.id for entry in entries] == [1, 2]
    assert [entry.payload.get("name") for entry in entries] == ["first", "second"]


def test_file_tape_store_excludes_sidecar_tapes(tmp_path) -> None:
    store = FileTapeStore(directory=tmp_path)
    sidecar = sidecar_tape_name("session__id", "spill")
    store.append("session__id", TapeEntry.event(name="main"))
    store.append(sidecar, TapeEntry.event(name="spill"))

    assert store.list_tapes() == ["session__id"]
