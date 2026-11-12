"""The same log contract applies to memory and persisted stores."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from bub.core.store import FileTapeStore, InMemoryTapeStore, TapeOffset, TapeQuery, TapeStore
from bub.core.tape import TapeEntry
from bub.errors import BubError

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    return InMemoryTapeStore() if request.param == "memory" else FileTapeStore(tmp_path, poll_interval=0.001)


async def names(store: TapeStore, **bounds) -> list[str]:
    async with store.stream("main", **bounds) as records:
        return [record.entry.payload["name"] async for record in records]


async def test_finite_cut_resume_and_append_receipts(store) -> None:
    beginning = await store.tail("main")
    first = await store.append("main", TapeEntry.event("first"))
    cut = await store.tail("main")
    second = await store.append("main", TapeEntry.event("second"))
    assert first.entry.id == 1
    assert second.entry.id == 2
    assert first.offset == cut
    assert await names(store, start=beginning, stop=cut) == ["first"]
    assert await names(store, start=first.offset, stop=second.offset) == ["second"]
    assert await names(store, start=cut, stop=cut) == []
    assert await names(store, stop=beginning) == []


async def test_query_captures_the_cut_on_entry(store) -> None:
    query = TapeQuery("main", store).stream()
    await store.append("main", TapeEntry.event("before-entry"))
    async with query as entries:
        await store.append("main", TapeEntry.event("during-query"))
        assert [entry.payload["name"] async for entry in entries] == ["before-entry"]


async def test_live_stream_replays_then_follows_without_losing_wakeups(store) -> None:
    await store.append("main", TapeEntry.event("history"))
    async with store.stream("main") as records:
        assert (await anext(records)).entry.payload["name"] == "history"
        for i in range(8):
            pending = asyncio.create_task(anext(records))
            await asyncio.sleep(0)
            receipt = await store.append("main", TapeEntry.event(str(i)))
            assert await asyncio.wait_for(pending, 2) == receipt


async def test_empty_follower_and_closing_one_reader_do_not_close_another(store) -> None:
    cut = await store.tail("main")
    async with store.stream("main", start=cut) as first, store.stream("main", start=cut) as second:
        pending = asyncio.create_task(anext(first))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        receipt = await store.append("main", TapeEntry.event("later"))
        assert await asyncio.wait_for(anext(second), 2) == receipt
    assert await names(store, stop=await store.tail("main")) == ["later"]


async def test_reset_invalidates_idle_followers_and_saved_offsets(store) -> None:
    old = await store.append("main", TapeEntry.event("old"))
    async with store.stream("main", start=old.offset) as stream:
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        await store.reset("main")
        await store.append("main", TapeEntry.event("new"))
        with pytest.raises(BubError, match="reset"):
            await asyncio.wait_for(pending, 2)
    with pytest.raises(BubError, match="offset"):
        await names(store, start=old.offset, stop=await store.tail("main"))
    assert await names(store, stop=await store.tail("main")) == ["new"]


async def test_invalid_offsets_are_rejected(store) -> None:
    start = await store.tail("main")
    end = (await store.append("main", TapeEntry.event("one"))).offset
    other = await store.tail("other")
    for left, right in [(end, start), (other, end), (TapeOffset("bad"), end)]:
        with pytest.raises(BubError):
            await names(store, start=left, stop=right)


async def test_offsets_survive_file_store_reopening(tmp_path: Path) -> None:
    first = FileTapeStore(tmp_path)
    receipt = await first.append("main", TapeEntry.event("one"))
    token = json.loads(json.dumps(receipt.offset.token))
    second = FileTapeStore(tmp_path)
    await second.append("main", TapeEntry.event("two"))
    assert await names(second, start=TapeOffset(token), stop=await second.tail("main")) == ["two"]


async def test_file_follower_waits_for_a_complete_line(tmp_path: Path) -> None:
    store = FileTapeStore(tmp_path, poll_interval=0.001)
    before = await store.tail("main")
    line = (json.dumps(asdict(TapeEntry(1, "event", {"name": "complete"}))) + "\n").encode()
    path = tmp_path / "main.jsonl"
    path.write_bytes(line[:-2])
    assert await store.tail("main") == before
    async with store.stream("main", start=before) as stream:
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        assert not pending.done()
        with pytest.raises(BubError, match="incomplete"):
            await store.append("main", TapeEntry.event("must-not-concatenate"))
        with path.open("ab") as handle:
            handle.write(line[-2:])
        record = await asyncio.wait_for(pending, 2)
        assert record.entry.payload["name"] == "complete"
        assert record.offset == await store.tail("main")


async def test_file_replacement_with_same_length_invalidates_offsets(tmp_path: Path) -> None:
    store = FileTapeStore(tmp_path)
    old = await store.append("main", TapeEntry.event("old"))
    path = tmp_path / "main.jsonl"
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes().replace(b'"old"', b'"new"'))
    replacement.replace(path)
    with pytest.raises(BubError):
        await names(store, start=old.offset, stop=await store.tail("main"))
    assert await names(store, stop=await store.tail("main")) == ["new"]


async def test_file_truncation_and_legacy_invalid_lines(tmp_path: Path) -> None:
    path = tmp_path / "main.jsonl"
    path.write_text('not-json\n[]\n{"id":7,"kind":"event","payload":{"name":"legacy"},"timestamp":0}\n')
    store = FileTapeStore(tmp_path)
    assert await names(store, stop=await store.tail("main")) == ["legacy"]
    last = await store.append("main", TapeEntry.event("next"))
    assert last.entry.id == 8
    path.write_bytes(b"")
    with pytest.raises(BubError):
        await names(store, stop=last.offset)
    assert await names(store, stop=await store.tail("main")) == []


async def test_file_store_serializes_its_own_appends_and_wakes_followers(tmp_path: Path) -> None:
    store = FileTapeStore(tmp_path, poll_interval=0.001)
    cut = await store.tail("main")
    async with store.stream("main", start=cut) as stream:
        pending = asyncio.create_task(anext(stream))
        receipts = await asyncio.wait_for(
            asyncio.gather(*(store.append("main", TapeEntry.event(str(i))) for i in range(12))), 5
        )
        first = await asyncio.wait_for(pending, 2)
    cut = await store.tail("main")
    async with store.stream("main", stop=cut) as stream:
        records = [record async for record in stream]
    assert [record.entry.id for record in records] == list(range(1, 13))
    assert first == records[0]
    assert sorted(receipts, key=lambda record: record.entry.id) == records


async def test_file_boundaries_handle_large_records_and_malformed_trailing_lines(tmp_path: Path) -> None:
    path = tmp_path / "main.jsonl"
    large = TapeEntry(41, "event", {"name": "large", "data": "x" * 25_000})
    path.write_text(json.dumps(asdict(large)) + "\nnot-json\n[]\n")
    store = FileTapeStore(tmp_path)
    cut = await store.tail("main")
    assert await names(store, stop=cut) == ["large"]
    receipt = await store.append("main", TapeEntry.event("next"))
    assert receipt.entry.id == 42
    assert await names(store, start=cut, stop=receipt.offset) == ["next"]
    assert await names(store, stop=receipt.offset) == ["large", "next"]
    with path.open("ab") as handle:
        handle.write(b"x" * 25_000)
    assert await store.tail("main") == receipt.offset
    with pytest.raises(BubError, match="incomplete"):
        await store.append("main", TapeEntry.event("rejected"))
