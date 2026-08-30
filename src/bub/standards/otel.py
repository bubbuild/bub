"""OpenTelemetry GenAI observer driven only by committed tape facts."""

from __future__ import annotations

from typing import Any

from opentelemetry.util.genai.handler import TelemetryHandler, get_telemetry_handler

from bub.tape import TapeRecord, bub_event_type, event_extension, event_payload

_STARTED = "started"
_TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})
_EVENTS = {
    bub_event_type(f"{operation}.{phase}"): (operation, phase)
    for operation in ("agent.invocation", "model", "tool")
    for phase in (_STARTED, *_TERMINAL_PHASES)
}


def _identifier(record: TapeRecord, name: str) -> str | None:
    value = event_extension(record.event, name)
    return str(value) if value is not None else None


def _bub_attributes(record: TapeRecord) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("session_id", "turn_id", "invocation_id", "model_call_id", "tool_call_id"):
        if value := _identifier(record, name):
            result[f"bub.{name.removesuffix('_id').replace('_', '.')}.id"] = value
    return result


def _set_usage(invocation: Any, usage: object) -> None:
    if not isinstance(usage, dict):
        return
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool):
        invocation.input_tokens = input_tokens
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        invocation.output_tokens = output_tokens


def _operation_error(record: TapeRecord) -> RuntimeError:
    error = event_payload(record.event).get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return RuntimeError(message)
    return RuntimeError(f"Bub operation ended as {record.event.get_type()}")


class OpenTelemetrySidecar:
    """Observe committed Bub runtime events through the official GenAI utility."""

    name = "otel"

    def __init__(self, handler: TelemetryHandler | None = None) -> None:
        self._handler = handler or get_telemetry_handler()
        self._active: dict[tuple[str, str, str], Any] = {}

    async def on_commit(self, tape: str, record: TapeRecord) -> None:
        event = _EVENTS.get(record.event.get_type())
        if event is None:
            return
        operation, phase = event
        operation_id = self._operation_id(operation, record)
        if operation_id is None:
            return
        key = (tape, operation, operation_id)
        if phase == _STARTED:
            self._active[key] = self._start(operation, record)
            return
        if phase not in _TERMINAL_PHASES or (invocation := self._active.pop(key, None)) is None:
            return

        if operation == "model":
            _set_usage(invocation, event_payload(record.event).get("usage"))
        elif operation == "tool" and invocation.should_capture_content_on_span:
            invocation.tool_result = event_payload(record.event).get("result")
        if phase == "completed":
            invocation.stop()
        else:
            invocation.fail(_operation_error(record))

    @staticmethod
    def _operation_id(operation: str, record: TapeRecord) -> str | None:
        if operation == "agent.invocation":
            return _identifier(record, "invocation_id")
        if operation == "model":
            return _identifier(record, "model_call_id")
        return _identifier(record, "tool_call_id")

    def _start(self, operation: str, record: TapeRecord) -> Any:
        invocation: Any
        data = event_payload(record.event)
        if operation == "agent.invocation":
            invocation = self._handler.invoke_local_agent(
                request_model=str(data.get("model") or ""),
                agent_name=str(data.get("agent") or "bub"),
            )
            invocation.agent_id = _identifier(record, "invocation_id")
            invocation.conversation_id = _identifier(record, "session_id")
        elif operation == "model":
            invocation = self._handler.inference(
                str(data.get("provider") or "custom"),
                request_model=str(data.get("model") or ""),
                operation_name="chat",
            )
        else:
            invocation = self._handler.tool(
                str(data.get("name") or "unknown"),
                tool_call_id=_identifier(record, "tool_call_id"),
                tool_type="function",
            )
            if invocation.should_capture_content_on_span:
                invocation.arguments = data.get("arguments")
        invocation.attributes.update(_bub_attributes(record))
        return invocation
