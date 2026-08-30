from __future__ import annotations

import json

import pytest

from bub.sidecars import sidecar_tape_name
from bub.store import FileTapeStore, ForkTapeStore, TapeQuery
from bub.tape import bub_event, bub_event_type
from bub.tape_codec import CloudEventJsonTapeCodec


@pytest.mark.asyncio
async def test_file_tape_store_assigns_monotonic_cursors_when_merging_forked_entries(tmp_path) -> None:
    parent = FileTapeStore(directory=tmp_path, codec=CloudEventJsonTapeCodec())
    store = ForkTapeStore(parent, "tape")

    await store.append("tape", bub_event(name="first", data={"n": 1}))
    await store.merge_back()

    store = ForkTapeStore(parent, "tape")
    await store.append("tape", bub_event(name="second", data={"n": 2}))
    await store.merge_back()

    records = [record async for record in parent.scan(TapeQuery("tape"))]
    assert [record.cursor for record in records] == [1, 2]
    assert [record.event.get_type() for record in records] == [bub_event_type("first"), bub_event_type("second")]


@pytest.mark.asyncio
async def test_file_tape_store_excludes_sidecar_tapes(tmp_path) -> None:
    store = FileTapeStore(directory=tmp_path, codec=CloudEventJsonTapeCodec())
    sidecar = sidecar_tape_name("session__id", "spill")
    await store.append("session__id", bub_event(name="main"))
    await store.append(sidecar, bub_event(name="spill"))

    assert await store.list_tapes() == ["session__id"]


@pytest.mark.asyncio
async def test_file_tape_store_writes_structured_cloud_event_with_atif_data_directly(tmp_path) -> None:
    store = FileTapeStore(directory=tmp_path, codec=CloudEventJsonTapeCodec())

    committed = await store.append("session__id", bub_event(name="diagnostic", data={"status": "ok"}))
    line = json.loads((tmp_path / "session__id.jsonl").read_text())

    assert line["specversion"] == "1.0"
    assert line["id"] == committed.event.get_id()
    assert line["data"]["step_id"] == committed.cursor == 1
    assert line["data"]["source"] == "system"
    assert line["data"]["observation"]["results"] == [{"content": '{"status":"ok"}'}]
    assert "cursor" not in line
    assert "event" not in line
