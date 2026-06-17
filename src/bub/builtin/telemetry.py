"""Telemetry adapters for projecting runtime tape-entry spans into tape streams."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from functools import cache
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

import logfire
from opentelemetry import context as otel_context

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

_TAPE_STORE_KEY = otel_context.create_key("bub.tape.store")
_TAPE_ENTRY_KEY = otel_context.create_key("bub.tape.entry")


class BubSpanContext:
    def __init__(
        self,
        name: str,
        attributes: dict[str, Any],
        store: object | None = None,
        entry: TapeEntry | None = None,
    ) -> None:
        self._name = name
        self._attributes = attributes
        self._store = store
        self._entry = entry
        self._manager: AbstractContextManager[Any] | None = None
        self._store_token: Any = None

    def __enter__(self) -> Any:
        ensure_telemetry_configured()
        span_context = otel_context.get_current()
        if self._store is not None and self._entry is not None:
            span_context = otel_context.set_value(_TAPE_STORE_KEY, self._store, span_context)
            span_context = otel_context.set_value(_TAPE_ENTRY_KEY, self._entry, span_context)
            self._store_token = otel_context.attach(span_context)
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
                otel_context.detach(self._store_token)
        return False


class TapeSpanExporter:
    """Project tape-entry spans into a tape store."""

    def __init__(self, store: object) -> None:
        self._store = store

    def export_entry(self, tape: str, entry: TapeEntry) -> None:
        append_nowait = getattr(self._store, "append_nowait", None)
        if callable(append_nowait):
            append_nowait(tape, entry)


def bub_span(
    name: str,
    *,
    tape: str | None = None,
    store: object | None = None,
    entry: TapeEntry | None = None,
    attributes: dict[str, Any] | None = None,
) -> BubSpanContext:
    span_attributes = dict(attributes or {})
    if tape:
        span_attributes[BUB_TAPE_NAME] = tape
    return BubSpanContext(name, span_attributes, store=store, entry=entry)


def tape_span_processor() -> SpanProcessor:
    """Return an OTel processor that writes Bub tape-entry spans into tapes."""

    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

    class TapeSpanProcessor(SpanProcessor):
        def on_start(self, span: object, parent_context: object | None = None) -> None:
            return

        def on_end(self, span: ReadableSpan) -> None:
            store = otel_context.get_value(_TAPE_STORE_KEY)
            entry = otel_context.get_value(_TAPE_ENTRY_KEY)
            tape = _span_tape_name(span)
            if store is not None and isinstance(entry, TapeEntry) and tape is not None:
                TapeSpanExporter(store).export_entry(tape, entry)

        def shutdown(self) -> None:
            return

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return TapeSpanProcessor()


@cache
def configure_telemetry() -> None:
    """Configure Logfire with Bub's tape span processor."""

    logfire.configure(
        send_to_logfire="if-token-present",
        console=False,
        inspect_arguments=False,
        additional_span_processors=[tape_span_processor()],
    )


def ensure_telemetry_configured() -> None:
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
    entry = TapeEntry(id=0, kind=kind, payload=dict(payload or {}), meta=dict(meta))
    attributes: dict[str, Any] = {
        BUB_TAPE_ENTRY_KIND: kind,
    }
    if run_id := meta.get("run_id"):
        attributes[BUB_RUN_ID] = str(run_id)
    with bub_span(f"bub.tape.{kind}", tape=tape, store=store, entry=entry, attributes=attributes):
        return


def _span_tape_name(span: object) -> str | None:
    attributes = getattr(span, "attributes", {})
    if not isinstance(attributes, Mapping):
        return None
    tape = attributes.get(BUB_TAPE_NAME)
    return tape if isinstance(tape, str) and tape else None
