"""OpenTelemetry adapters for writing selected spans into tape streams."""

from __future__ import annotations

import contextvars
from collections.abc import Mapping
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Literal, Protocol, cast

from bub.tape import TapeEntry

BUB_TAPE_NAME = "bub.tape.name"
BUB_RUN_ID = "bub.run.id"
BUB_AGENT_NAME = "bub"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_AGENT_NAME = "gen_ai.agent.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
OTEL_SPAN_ENTRY_KIND = "otel.span"


class TapeStreamWriter(Protocol):
    def append_nowait(self, tape: str, entry: TapeEntry) -> None: ...


class RuntimeSpan(Protocol):
    def set_attribute(self, key: str, value: object) -> None: ...

    def record_exception(self, exception: BaseException) -> None: ...


_current_writer: contextvars.ContextVar[TapeStreamWriter | None] = contextvars.ContextVar(
    "bub_tape_stream_writer",
    default=None,
)
_current_tape: contextvars.ContextVar[str | None] = contextvars.ContextVar("bub_tape_name", default=None)


class NoopSpan:
    def set_attribute(self, key: str, value: object) -> None:
        return

    def record_exception(self, exception: BaseException) -> None:
        return


class TapeWriterBinding:
    def __init__(self, writer: TapeStreamWriter, tape: str) -> None:
        self._writer = writer
        self._tape = tape
        self._previous_writer: TapeStreamWriter | None = None
        self._previous_tape: str | None = None

    def __enter__(self) -> None:
        self._previous_writer = _current_writer.get()
        self._previous_tape = _current_tape.get()
        _current_writer.set(self._writer)
        _current_tape.set(self._tape)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        _current_tape.set(self._previous_tape)
        _current_writer.set(self._previous_writer)
        return False


def bind_tape_writer(writer: TapeStreamWriter, tape: str) -> TapeWriterBinding:
    return TapeWriterBinding(writer, tape)


class BubSpanContext:
    def __init__(self, name: str, attributes: dict[str, object]) -> None:
        self._manager = _start_span(name)
        self._attributes = attributes
        self._span: RuntimeSpan | None = None

    def __enter__(self) -> RuntimeSpan:
        span = self._manager.__enter__()
        self._span = span
        _set_span_attributes(span, self._attributes)
        return span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if exc is not None and self._span is not None:
            _record_span_exception(self._span, exc)
        self._manager.__exit__(None, None, None)
        return False


def bub_span(
    name: str,
    *,
    tape: str | None = None,
    attributes: dict[str, object] | None = None,
) -> BubSpanContext:
    span_attributes = dict(attributes or {})
    if tape_name := tape or _current_tape.get():
        span_attributes[BUB_TAPE_NAME] = tape_name
    return BubSpanContext(name, span_attributes)


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


def _start_span(name: str) -> AbstractContextManager[RuntimeSpan]:
    try:
        from opentelemetry import trace
    except Exception:
        return NoopSpanContext()
    return cast(AbstractContextManager[RuntimeSpan], trace.get_tracer("bub.agent").start_as_current_span(name))


class NoopSpanContext:
    def __enter__(self) -> RuntimeSpan:
        return NoopSpan()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        return False


def _set_span_attributes(span: RuntimeSpan, attributes: dict[str, object]) -> None:
    set_attribute = getattr(span, "set_attribute", None)
    if not callable(set_attribute):
        return
    for key, value in attributes.items():
        if _is_span_attribute(value):
            set_attribute(key, value)


def _record_span_exception(span: RuntimeSpan, exc: BaseException) -> None:
    record_exception = getattr(span, "record_exception", None)
    if callable(record_exception):
        record_exception(exc)
    set_attribute = getattr(span, "set_attribute", None)
    if callable(set_attribute):
        set_attribute("error.type", exc.__class__.__name__)


def _is_span_attribute(value: object) -> bool:
    if isinstance(value, bool | str | bytes | int | float):
        return True
    if isinstance(value, list | tuple):
        return all(isinstance(item, bool | str | bytes | int | float) for item in value)
    return False


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
