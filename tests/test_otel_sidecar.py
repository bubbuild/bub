from __future__ import annotations

from typing import Any

import pytest

from bub.standards import OpenTelemetrySidecar
from bub.store import InMemoryTapeStore
from bub.tape import Tape, TapeContext


class _Invocation:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.should_capture_content_on_span = True
        self.arguments: Any = None
        self.tool_result: Any = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.agent_id: str | None = None
        self.conversation_id: str | None = None
        self.stopped = False
        self.error: BaseException | None = None

    def stop(self) -> None:
        self.stopped = True

    def fail(self, error: BaseException) -> None:
        self.error = error


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], _Invocation]] = []

    def _call(self, operation: str, arguments: dict[str, Any]) -> _Invocation:
        invocation = _Invocation()
        self.calls.append((operation, arguments, invocation))
        return invocation

    def invoke_local_agent(self, **arguments: Any) -> _Invocation:
        return self._call("agent", arguments)

    def inference(self, provider: str, **arguments: Any) -> _Invocation:
        return self._call("model", {"provider": provider, **arguments})

    def tool(self, name: str, **arguments: Any) -> _Invocation:
        return self._call("tool", {"name": name, **arguments})


@pytest.mark.asyncio
async def test_otel_sidecar_observes_committed_runtime_events() -> None:
    handler = _Handler()
    sidecar = OpenTelemetrySidecar(handler)  # type: ignore[arg-type]
    state = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "invocation_id": "invocation-1",
        "model_call_id": "model-1",
        "tool_call_id": "tool-1",
    }
    tape = Tape(InMemoryTapeStore(), TapeContext(anchor=None, state=state), sidecars=(sidecar,)).scoped("tape")

    await tape.record_operation("agent.invocation", "started", {"agent": "bub", "model": "openai:test"})
    await tape.record_operation("model", "started", {"provider": "openai", "model": "test"})
    await tape.record_operation("model", "completed", {"usage": {"prompt_tokens": 8, "completion_tokens": 3}})
    await tape.record_operation("tool", "started", {"name": "search", "arguments": {"query": "bub"}})
    await tape.record_operation("tool", "completed", {"name": "search", "result": "found"})
    await tape.record_operation("agent.invocation", "completed", {})

    agent = handler.calls[0][2]
    model = handler.calls[1][2]
    tool = handler.calls[2][2]
    assert agent.agent_id == "invocation-1"
    assert agent.conversation_id == "session-1"
    assert model.input_tokens == 8
    assert model.output_tokens == 3
    assert tool.arguments == {"query": "bub"}
    assert tool.tool_result == "found"
    assert agent.stopped and model.stopped and tool.stopped
