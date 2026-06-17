"""Telemetry adapters for projecting runtime tape-entry spans into tape streams."""

from __future__ import annotations

import contextvars
import json
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Protocol

import logfire

from bub.tape import TapeEntry

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import SpanProcessor

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
BUB_TAPE_ENTRY_KIND = "bub.tape.entry.kind"
BUB_TAPE_ENTRY_PAYLOAD = "bub.tape.entry.payload_json"
BUB_TAPE_ENTRY_META = "bub.tape.entry.meta_json"

_current_tape_store: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "bub_tape_store",
    default=None,
)
_telemetry_configured = False


class RuntimeSpan(Protocol):
    def set_attribute(self, key: str, value: object) -> None: ...

    def record_exception(self, exception: BaseException) -> None: ...


class BubSpanContext:
    def __init__(self, name: str, attributes: dict[str, Any], store: object | None = None) -> None:
        self._name = name
        self._attributes = attributes
        self._store = store
        self._manager: AbstractContextManager[RuntimeSpan] | None = None
        self._store_token: contextvars.Token[object | None] | None = None

    def __enter__(self) -> RuntimeSpan:
        ensure_telemetry_configured()
        if self._store is not None:
            self._store_token = _current_tape_store.set(self._store)
        self._manager = logfire.span(self._name, _span_name=self._name, **self._attributes)
        return self._manager.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            if self._manager is not None:
                self._manager.__exit__(exc_type, exc, traceback)
        finally:
            if self._store_token is not None:
                _current_tape_store.reset(self._store_token)
        return False


class TapeSpanExporter:
    """Project tape-entry spans into a tape store."""

    def __init__(self, store: object) -> None:
        self._store = store

    def export_span(self, span: object) -> None:
        tape = _span_tape_name(span)
        entry = span_to_tape_entry(span)
        if tape is None or entry is None:
            return

        append_nowait = getattr(self._store, "append_nowait", None)
        if callable(append_nowait):
            append_nowait(tape, entry)


def bub_span(
    name: str,
    *,
    tape: str | None = None,
    store: object | None = None,
    attributes: dict[str, Any] | None = None,
) -> BubSpanContext:
    span_attributes = dict(attributes or {})
    if tape:
        span_attributes[BUB_TAPE_NAME] = tape
    return BubSpanContext(name, span_attributes, store=store)


def tape_span_processor() -> SpanProcessor:
    """Return an OTel processor that writes Bub tape-entry spans into tapes."""

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


def configure_telemetry(additional_span_processors: Iterable[SpanProcessor] = ()) -> None:
    """Configure Logfire with Bub's tape span processor."""

    global _telemetry_configured
    if _telemetry_configured:
        return

    logfire.configure(
        send_to_logfire="if-token-present",
        inspect_arguments=False,
        additional_span_processors=[tape_span_processor(), *additional_span_processors],
    )
    _telemetry_configured = True


def ensure_telemetry_configured() -> None:
    if not _telemetry_configured:
        configure_telemetry()


def loguru_handler() -> Any:
    return logfire.loguru_handler()


def record_tape_entry(
    store: object,
    tape: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    **meta: Any,
) -> None:
    attributes: dict[str, Any] = {
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
    kind = attributes.get(BUB_TAPE_ENTRY_KIND)
    if not isinstance(kind, str) or not kind:
        return None
    payload = _json_mapping(attributes.get(BUB_TAPE_ENTRY_PAYLOAD))
    meta = _json_mapping(attributes.get(BUB_TAPE_ENTRY_META))
    return TapeEntry(id=0, kind=kind, payload=payload, meta=meta)


def _span_tape_name(span: object) -> str | None:
    tape = _mapping(getattr(span, "attributes", None)).get(BUB_TAPE_NAME)
    return tape if isinstance(tape, str) and tape else None


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
