"""Telemetry adapters for projecting runtime spans into tape streams."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Literal, Protocol, cast

from bub.tape import TapeEntry

SpanAttributeValue = str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]

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
BUB_TAPE_ENTRY_KIND = "bub.tape.entry.kind"
BUB_TAPE_ENTRY_PAYLOAD = "bub.tape.entry.payload_json"
BUB_TAPE_ENTRY_META = "bub.tape.entry.meta_json"

_current_tape_store: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "bub_tape_store",
    default=None,
)
_tape_span_processor_configured = False
_pending_append_tasks: set[asyncio.Task[Any]] = set()


class RuntimeSpan(Protocol):
    def set_attribute(self, key: str, value: object) -> None: ...

    def record_exception(self, exception: BaseException) -> None: ...


class NoopSpan:
    def set_attribute(self, key: str, value: object) -> None:
        return

    def record_exception(self, exception: BaseException) -> None:
        return


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


class BubSpanContext:
    def __init__(self, name: str, attributes: dict[str, object], store: object | None = None) -> None:
        self._name = name
        self._attributes = attributes
        self._store = store
        self._manager: AbstractContextManager[RuntimeSpan] | None = None
        self._store_token: contextvars.Token[object | None] | None = None
        self._span: RuntimeSpan | None = None

    def __enter__(self) -> RuntimeSpan:
        ensure_telemetry_configured()
        if self._store is not None:
            self._store_token = _current_tape_store.set(self._store)
        self._manager = _start_span(self._name, self._attributes)
        self._span = self._manager.__enter__()
        return self._span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if exc is not None and self._span is not None:
            _record_span_exception(self._span, exc)
        if self._manager is not None:
            self._manager.__exit__(None, None, None)
        if self._store_token is not None:
            _current_tape_store.reset(self._store_token)
        return False


def bub_span(
    name: str,
    *,
    tape: str | None = None,
    store: object | None = None,
    attributes: dict[str, object] | None = None,
) -> BubSpanContext:
    span_attributes = dict(attributes or {})
    if tape:
        span_attributes[BUB_TAPE_NAME] = tape
    return BubSpanContext(name, span_attributes, store=store)


class TapeSpanExporter:
    """Project selected telemetry spans into a tape store."""

    def __init__(self, store: object) -> None:
        self._store = store

    def export_span(self, span: object) -> None:
        entry = span_to_tape_entry(span)
        if entry is None:
            return

        tape = _span_tape_name(span)
        if tape is None:
            return

        _append_entry(self._store, tape, entry)


def tape_span_processor() -> object:
    """Return an OpenTelemetry span processor that writes selected spans into tapes."""

    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

    class TapeSpanProcessor(SpanProcessor):
        def on_start(self, span: object, parent_context: object | None = None) -> None:
            return

        def on_end(self, span: ReadableSpan) -> None:
            store = _current_tape_store.get()
            if store is not None:
                TapeSpanExporter(store).export_span(span)

        def shutdown(self) -> None:
            return

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return TapeSpanProcessor()


def configure_telemetry(additional_span_processors: Iterable[object] = ()) -> None:
    """Install Bub's default tape span processor on the active OTel provider."""

    provider = _ensure_tracer_provider()
    if provider is None:
        return

    global _tape_span_processor_configured
    add_span_processor = getattr(provider, "add_span_processor", None)
    if not callable(add_span_processor):
        return

    processors = list(additional_span_processors)
    if not _tape_span_processor_configured:
        processors = processors or [tape_span_processor()]
        for processor in processors:
            add_span_processor(processor)
        _tape_span_processor_configured = True
        return

    for processor in processors:
        add_span_processor(processor)


def mark_tape_span_processor_configured() -> None:
    global _tape_span_processor_configured
    _tape_span_processor_configured = True


def ensure_telemetry_configured() -> None:
    if not _tape_span_processor_configured:
        configure_telemetry()


def record_tape_entry(
    store: object,
    tape: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    **meta: Any,
) -> None:
    attributes: dict[str, object] = {
        BUB_TAPE_ENTRY_KIND: kind,
        BUB_TAPE_ENTRY_PAYLOAD: json.dumps(payload or {}, ensure_ascii=False, default=str),
        BUB_TAPE_ENTRY_META: json.dumps(meta, ensure_ascii=False, default=str),
    }
    if run_id := meta.get("run_id"):
        attributes[BUB_RUN_ID] = str(run_id)
    with bub_span(f"bub.tape.{kind}", tape=tape, store=store, attributes=attributes):
        return


def span_to_tape_entry(span: object) -> TapeEntry | None:
    attributes = _mapping(getattr(span, "attributes", None))
    tape = attributes.get(BUB_TAPE_NAME)
    if not isinstance(tape, str) or not tape:
        return None

    if entry := _stream_entry_from_span_attributes(attributes):
        return entry

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


def _start_span(name: str, attributes: dict[str, object]) -> AbstractContextManager[RuntimeSpan]:
    try:
        from opentelemetry import trace
    except Exception:
        return NoopSpanContext()
    return cast(
        AbstractContextManager[RuntimeSpan],
        trace.get_tracer("bub.agent").start_as_current_span(name, attributes=_span_attributes(attributes)),
    )


def _ensure_tracer_provider() -> object | None:
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
    except Exception:
        return None

    provider = trace.get_tracer_provider()
    if callable(getattr(provider, "add_span_processor", None)):
        return provider

    try:
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    except Exception:
        provider = trace.get_tracer_provider()
    return provider if callable(getattr(provider, "add_span_processor", None)) else None


def _record_span_exception(span: RuntimeSpan, exc: BaseException) -> None:
    record_exception = getattr(span, "record_exception", None)
    if callable(record_exception):
        record_exception(exc)
    set_attribute = getattr(span, "set_attribute", None)
    if callable(set_attribute):
        set_attribute("error.type", exc.__class__.__name__)


def _append_entry(store: object, tape: str, entry: TapeEntry) -> None:
    append_nowait = getattr(store, "append_nowait", None)
    if callable(append_nowait):
        append_nowait(tape, entry)
        return

    append = getattr(store, "append", None)
    if not callable(append):
        return

    result = append(tape, entry)
    if not inspect.isawaitable(result):
        return

    coro = cast(Coroutine[Any, Any, Any], result)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
    else:
        task: asyncio.Task[Any] = loop.create_task(coro)
        _pending_append_tasks.add(task)
        task.add_done_callback(_pending_append_tasks.discard)


def _span_tape_name(span: object) -> str | None:
    tape = _mapping(getattr(span, "attributes", None)).get(BUB_TAPE_NAME)
    return tape if isinstance(tape, str) and tape else None


def _stream_entry_from_span_attributes(attributes: dict[str, Any]) -> TapeEntry | None:
    kind = attributes.get(BUB_TAPE_ENTRY_KIND)
    if not isinstance(kind, str) or not kind:
        return None
    payload = _json_mapping(attributes.get(BUB_TAPE_ENTRY_PAYLOAD))
    meta = _json_mapping(attributes.get(BUB_TAPE_ENTRY_META))
    return TapeEntry(id=0, kind=kind, payload=payload, meta=meta)


def _json_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _span_attributes(attributes: dict[str, object]) -> dict[str, SpanAttributeValue]:
    result: dict[str, SpanAttributeValue] = {}
    for key, value in attributes.items():
        if isinstance(value, str | bool | int | float):
            result[key] = value
            continue
        sequence = _span_sequence_attribute(value)
        if sequence is not None:
            result[key] = sequence
    return result


def _span_sequence_attribute(value: object) -> SpanAttributeValue | None:
    if not isinstance(value, list | tuple):
        return None
    if all(isinstance(item, str) for item in value):
        return list(value)
    if all(isinstance(item, bool) for item in value):
        return list(value)
    if all(isinstance(item, int) for item in value):
        return list(value)
    if all(isinstance(item, float) for item in value):
        return list(value)
    return None


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
