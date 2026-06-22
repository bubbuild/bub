from __future__ import annotations

import pytest

from bub.builtin.store import ForkTapeStore
from bub.builtin.telemetry import TapeSpanExporter, record_tape_entry
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeEntry


@pytest.mark.asyncio
async def test_tape_span_exporter_writes_entry_to_tape() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")
    entry = TapeEntry(id=0, kind="event", payload={"name": "step", "data": {"value": 1}}, meta={"run_id": "run-1"})

    TapeSpanExporter(store).export_entry("ops", entry)

    await store.merge_back()

    entries = parent.read("ops") or []
    assert [(item.kind, item.payload, item.meta) for item in entries] == [
        ("event", {"name": "step", "data": {"value": 1}}, {"run_id": "run-1"})
    ]


@pytest.mark.asyncio
async def test_record_tape_entry_writes_stream_entry_through_logfire_processor() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")

    await record_tape_entry(store, "ops", "event", {"name": "step", "data": {"value": 1}}, run_id="run-1")

    await store.merge_back()

    entries = parent.read("ops") or []
    assert [(entry.kind, entry.payload, entry.meta) for entry in entries] == [
        ("event", {"name": "step", "data": {"value": 1}}, {"run_id": "run-1"})
    ]


@pytest.mark.asyncio
async def test_tape_entry_payload_is_not_scrubbed_by_logfire_attributes() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")

    await record_tape_entry(
        store,
        "ops",
        "event",
        {"name": "command", "data": {"output": "session id and secret must stay in tape"}},
    )

    await store.merge_back()

    entries = parent.read("ops") or []
    assert entries[0].payload["data"]["output"] == "session id and secret must stay in tape"
