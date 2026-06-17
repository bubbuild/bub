from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from bub.builtin.store import ForkTapeStore
from bub.builtin.telemetry import BUB_RUN_ID, BUB_TAPE_NAME, TapeSpanExporter, bind_tape_writer, span_to_tape_entry
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore


@dataclass(frozen=True)
class FakeSpanContext:
    trace_id: int
    span_id: int


@dataclass(frozen=True)
class FakeSpanEvent:
    name: str
    timestamp: int
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FakeStatusCode:
    name: str


@dataclass(frozen=True)
class FakeStatus:
    status_code: FakeStatusCode
    description: str | None = None


@dataclass(frozen=True)
class FakeSpan:
    name: str
    context: FakeSpanContext
    attributes: dict[str, Any]
    parent: FakeSpanContext | None = None
    events: tuple[FakeSpanEvent, ...] = ()
    status: FakeStatus | None = None
    start_time: int = 1
    end_time: int = 2


def test_span_to_tape_entry_projects_otel_span_into_stream_record() -> None:
    span = FakeSpan(
        name="chat model",
        context=FakeSpanContext(trace_id=0x1234, span_id=0x5678),
        parent=FakeSpanContext(trace_id=0x1234, span_id=0x9999),
        attributes={BUB_TAPE_NAME: "ops", BUB_RUN_ID: "run-1", "gen_ai.operation.name": "chat"},
        events=(FakeSpanEvent("chunk", 10, {"size": 3}),),
        status=FakeStatus(FakeStatusCode("OK")),
    )

    entry = span_to_tape_entry(span)

    assert entry is not None
    assert entry.kind == "otel.span"
    assert entry.meta == {
        "tape": "ops",
        "trace_id": "00000000000000000000000000001234",
        "span_id": "0000000000005678",
        "run_id": "run-1",
    }
    assert entry.payload["name"] == "chat model"
    assert entry.payload["parent_span_id"] == "0000000000009999"
    assert entry.payload["attributes"]["gen_ai.operation.name"] == "chat"
    assert entry.payload["events"] == [{"name": "chunk", "timestamp": 10, "attributes": {"size": 3}}]
    assert entry.payload["status"] == {"status_code": "OK", "description": None}


def test_span_without_tape_name_is_not_projected() -> None:
    span = FakeSpan(name="debug", context=FakeSpanContext(trace_id=1, span_id=2), attributes={})

    assert span_to_tape_entry(span) is None


@pytest.mark.asyncio
async def test_tape_span_exporter_writes_to_bound_matching_fork() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")
    span = FakeSpan(
        name="chat model",
        context=FakeSpanContext(trace_id=1, span_id=2),
        attributes={BUB_TAPE_NAME: "ops"},
    )

    with bind_tape_writer(store, "ops"):
        TapeSpanExporter().export_span(span)

    await store.merge_back()

    entries = parent.read("ops") or []
    assert [entry.kind for entry in entries] == ["otel.span"]
    assert entries[0].payload["trace_id"] == "00000000000000000000000000000001"


@pytest.mark.asyncio
async def test_tape_span_exporter_ignores_unbound_tape() -> None:
    parent = InMemoryTapeStore()
    store = ForkTapeStore(AsyncTapeStoreAdapter(parent), "ops")
    span = FakeSpan(
        name="chat model",
        context=FakeSpanContext(trace_id=1, span_id=2),
        attributes={BUB_TAPE_NAME: "other"},
    )

    with bind_tape_writer(store, "ops"):
        TapeSpanExporter().export_span(span)

    await store.merge_back()

    assert parent.read("ops") is None
