"""OpenTelemetry adapters for writing selected spans into tape streams."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator, Mapping
from typing import Any, Protocol

from bub.tape import TapeEntry

BUB_TAPE_NAME = "bub.tape.name"
BUB_RUN_ID = "bub.run.id"
OTEL_SPAN_ENTRY_KIND = "otel.span"


class TapeStreamWriter(Protocol):
    def append_nowait(self, tape: str, entry: TapeEntry) -> None: ...


_current_writer: contextvars.ContextVar[TapeStreamWriter | None] = contextvars.ContextVar(
    "bub_tape_stream_writer",
    default=None,
)
_current_tape: contextvars.ContextVar[str | None] = contextvars.ContextVar("bub_tape_name", default=None)


@contextlib.contextmanager
def bind_tape_writer(writer: TapeStreamWriter, tape: str) -> Iterator[None]:
    previous_writer = _current_writer.get()
    previous_tape = _current_tape.get()
    _current_writer.set(writer)
    _current_tape.set(tape)
    try:
        yield
    finally:
        _current_tape.set(previous_tape)
        _current_writer.set(previous_writer)


class TapeSpanExporter:
    """Project selected OTel spans into the currently bound tape stream."""

    def export_span(self, span: object) -> None:
        writer = _current_writer.get()
        if writer is None:
            return

        entry = span_to_tape_entry(span)
        if entry is None:
            return

        tape = entry.meta.get("tape")
        if not isinstance(tape, str) or tape != _current_tape.get():
            return

        writer.append_nowait(tape, entry)


def tape_span_processor() -> object:
    """Return an OpenTelemetry span processor for Logfire configuration."""

    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

    exporter = TapeSpanExporter()

    class TapeSpanProcessor(SpanProcessor):
        def on_start(self, span: object, parent_context: object | None = None) -> None:
            return

        def on_end(self, span: ReadableSpan) -> None:
            exporter.export_span(span)

        def shutdown(self) -> None:
            return

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return TapeSpanProcessor()


def span_to_tape_entry(span: object) -> TapeEntry | None:
    attributes = _mapping(getattr(span, "attributes", None))
    tape = attributes.get(BUB_TAPE_NAME)
    if not isinstance(tape, str) or not tape:
        return None

    context = getattr(span, "context", None)
    trace_id = _trace_id(getattr(context, "trace_id", None))
    span_id = _span_id(getattr(context, "span_id", None))
    parent = getattr(span, "parent", None)
    parent_span_id = _span_id(getattr(parent, "span_id", None))

    payload: dict[str, Any] = {
        "name": str(getattr(span, "name", "")),
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "start_time": getattr(span, "start_time", None),
        "end_time": getattr(span, "end_time", None),
        "attributes": attributes,
        "events": [_span_event(event) for event in getattr(span, "events", ())],
        "status": _span_status(getattr(span, "status", None)),
    }
    meta = {"tape": tape, "trace_id": trace_id, "span_id": span_id}
    if run_id := attributes.get(BUB_RUN_ID):
        meta["run_id"] = str(run_id)
    return TapeEntry(id=0, kind=OTEL_SPAN_ENTRY_KIND, payload=payload, meta=meta)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _span_event(event: object) -> dict[str, Any]:
    return {
        "name": str(getattr(event, "name", "")),
        "timestamp": getattr(event, "timestamp", None),
        "attributes": _mapping(getattr(event, "attributes", None)),
    }


def _span_status(status: object) -> dict[str, Any]:
    if status is None:
        return {}
    status_code = getattr(status, "status_code", None)
    code_name = getattr(status_code, "name", None)
    return {
        "status_code": str(code_name or status_code or ""),
        "description": getattr(status, "description", None),
    }


def _trace_id(value: object) -> str | None:
    if not isinstance(value, int):
        return None
    return f"{value:032x}"


def _span_id(value: object) -> str | None:
    if not isinstance(value, int):
        return None
    return f"{value:016x}"
