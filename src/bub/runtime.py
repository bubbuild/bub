"""Runtime data contracts shared by Bub core, channels, and plugins."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class ErrorKind(StrEnum):
    """Stable error kinds for runtime decisions."""

    INVALID_INPUT = "invalid_input"
    CONFIG = "config"
    PROVIDER = "provider"
    TOOL = "tool"
    TEMPORARY = "temporary"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BubError(Exception):
    """Public error type for Bub runtime failures."""

    kind: ErrorKind
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"[{self.kind.value}] {self.message}"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class RuntimeChoice:
    """One selectable runtime value."""

    id: str
    name: str | None = None
    description: str | None = None
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class RuntimeOptions:
    """Runtime choices that a channel or adapter may present to a user."""

    models: list[RuntimeChoice] = field(default_factory=list)
    current_model: str | None = None


@dataclass(frozen=True)
class LlmCallRequest:
    """Outgoing agent-loop LLM request exposed to interception hooks.

    Hooks may return a modified copy (``dataclasses.replace``) to change the
    model or messages for this call. Tool objects are not exposed;
    ``tool_names`` is observational and altering the toolset is out of scope.
    """

    run_id: str
    model: str
    messages: list[dict[str, Any]]
    tool_names: tuple[str, ...] = ()
    max_tokens: int | None = None


@dataclass(frozen=True)
class LlmCallResult:
    """Terminal outcome of one LLM call exposed to interception hooks.

    For streaming completions this is the fully accumulated final state, not
    a per-chunk view. ``error`` is the original raised exception (and other
    fields are best effort) when the call failed. Cancellation and consumer
    close are not observed.
    """

    run_id: str
    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    error: Exception | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class LlmCallDecision:
    """Short-circuit verdict returned by a ``before_llm_call`` hook."""

    action: Literal["finish"] = "finish"
    text: str = ""

    @classmethod
    def finish(cls, text: str) -> LlmCallDecision:
        """Skip the provider call and emit ``text`` as its final output."""

        return cls(action="finish", text=text)


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation exposed to interception hooks."""

    run_id: str
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallDecision:
    """Verdict returned by a ``before_tool_call`` hook.

    ``proceed`` continues with optional argument changes. ``replace`` skips
    the handler and uses the supplied result. ``deny`` skips the handler and
    surfaces the supplied message as a tool error.
    """

    action: Literal["proceed", "replace", "deny"] = "proceed"
    arguments: dict[str, Any] | None = None
    result: Any = None
    message: str | None = None

    @classmethod
    def proceed(cls, arguments: dict[str, Any] | None = None) -> ToolCallDecision:
        return cls(action="proceed", arguments=arguments)

    @classmethod
    def replace(cls, result: Any) -> ToolCallDecision:
        return cls(action="replace", result=result)

    @classmethod
    def deny(cls, message: str) -> ToolCallDecision:
        return cls(action="deny", message=message)


@dataclass(frozen=True)
class ToolCallResult:
    """Terminal outcome of one tool invocation exposed to hooks."""

    run_id: str
    tool: str
    arguments: dict[str, Any]
    result: Any = None
    error: Exception | None = None
    duration_ms: int = 0


@dataclass
class StreamState:
    error: BubError | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class StreamEvent:
    kind: Literal["text", "reasoning", "tool_call", "tool_result", "usage", "error", "final"]
    data: dict[str, Any]


class AsyncStreamEvents:
    def __init__(self, iterator: AsyncIterator[StreamEvent], *, state: StreamState | None = None) -> None:
        self._iterator = iterator
        self._state = state or StreamState()

    def __aiter__(self) -> AsyncIterator[StreamEvent]:
        return self._iterator

    @property
    def error(self) -> BubError | None:
        return self._state.error

    @property
    def usage(self) -> dict[str, Any] | None:
        return self._state.usage
