from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from bub.builtin.store import ForkTapeStore
from bub.builtin.telemetry import (
    BUB_TAPE_ENTRY_KIND,
    BUB_TAPE_ENTRY_META,
    BUB_TAPE_ENTRY_PAYLOAD,
    BUB_TAPE_NAME,
    TapeSpanExporter,
    record_tape_entry,
    span_to_tape_entry,
)
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore


@dataclass(frozen=True)
class FakeSpan:
    attributes: dict[str, Any]


def test_span_to_tape_entry_projects_tape_entry_span_without_routing_meta() -> None:
    span = FakeSpan(
        attributes={
            BUB_TAPE_NAME: "ops",
            BUB_TAPE_ENTRY_KIND: "message",
            BUB_TAPE_ENTRY_PAYLOAD: '{"role": "user", "content": "hello"}',
            BUB_TAPE_ENTRY_META: '{"run_id": "run-1"}',
        },
    )

    entry = span_to_tape_entry(span)

    assert entry is not None
    assert entry.kind == "message"
    assert entry.payload == {"role": "user", "content": "hello"}
    assert entry.meta == {"run_id": "run-1"}


def test_regular_observability_span_is_not_projected_to_tape() -> None:
    span = FakeSpan(attributes={BUB_TAPE_NAME: "ops", "gen_ai.operation.name": "chat"})

    assert span_to_tape_entry(span) is None


@pytest.mark.asyncio
async def test_tape_span_exporter_writes_tape_entry_span_to_span_tape() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")
    span = FakeSpan(
        attributes={
            BUB_TAPE_NAME: "ops",
            BUB_TAPE_ENTRY_KIND: "event",
            BUB_TAPE_ENTRY_PAYLOAD: '{"name": "step", "data": {"value": 1}}',
            BUB_TAPE_ENTRY_META: '{"run_id": "run-1"}',
        },
    )

    TapeSpanExporter(store).export_span(span)

    await store.merge_back()

    entries = parent.read("ops") or []
    assert [(entry.kind, entry.payload, entry.meta) for entry in entries] == [
        ("event", {"name": "step", "data": {"value": 1}}, {"run_id": "run-1"})
    ]


@pytest.mark.asyncio
async def test_record_tape_entry_writes_stream_entry_through_logfire_processor() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")

    record_tape_entry(store, "ops", "event", {"name": "step", "data": {"value": 1}}, run_id="run-1")

    await store.merge_back()

    entries = parent.read("ops") or []
    assert [(entry.kind, entry.payload, entry.meta) for entry in entries] == [
        ("event", {"name": "step", "data": {"value": 1}}, {"run_id": "run-1"})
    ]


@pytest.mark.asyncio
async def test_tape_span_exporter_ignores_span_without_tape_name() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")
    span = FakeSpan(
        attributes={
            BUB_TAPE_ENTRY_KIND: "event",
            BUB_TAPE_ENTRY_PAYLOAD: '{"name": "step"}',
        },
    )

    TapeSpanExporter(store).export_span(span)

    await store.merge_back()

    assert parent.read("ops") is None
