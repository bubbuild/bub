"""Agent-loop interception payload contracts (issue #253).

These dataclasses are the generic shapes passed to the ``before_llm_call`` /
``after_llm_call`` / ``before_tool_call`` / ``after_tool_call`` hookspecs.
They carry no chat or provider business beyond what the hooks need to
observe or modify; the execution semantics live in
:class:`bub.hook_runtime.AgentHooks`.

``run_id`` is threaded through every payload so plugins can correlate hook
observations with tape entries (which carry the same ``run_id`` meta).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class LlmCallRequest:
    """Outgoing agent-loop LLM request, as seen by ``before_llm_call``.

    Hooks may return a modified copy (``dataclasses.replace``) to change the
    model or messages for this call. Tool *objects* are not exposed —
    ``tool_names`` is observational; altering the toolset is out of scope.
    """

    run_id: str
    model: str
    messages: list[dict[str, Any]]
    tool_names: tuple[str, ...] = ()
    max_tokens: int | None = None


@dataclass(frozen=True)
class LlmCallResult:
    """Terminal outcome of one LLM call, as seen by ``after_llm_call``.

    For streaming completions this is the fully-accumulated final state,
    not a per-chunk view. ``error`` is the original raised exception (and
    other fields best-effort) when the call failed. Cancellation and
    consumer close are not observed: after hooks fire only for real
    completions and ``Exception`` failures.
    """

    run_id: str
    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    error: Exception | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class LlmCallDecision:
    """Short-circuit verdict returned by ``before_llm_call``.

    ``finish`` skips the provider call entirely and emits ``text`` as the
    final assistant output for this call — the cost-guard / call-limit
    primitive (cf. LangChain ``ModelCallLimitMiddleware``).
    """

    action: Literal["finish"] = "finish"
    text: str = ""

    @classmethod
    def finish(cls, text: str) -> LlmCallDecision:
        return cls(action="finish", text=text)


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation about to run, as seen by ``before_tool_call``."""

    run_id: str
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallDecision:
    """Verdict returned by a ``before_tool_call`` implementation.

    - ``proceed``: continue (optionally with modified ``arguments``);
      later hook implementations still run and see the updated call.
    - ``replace``: skip the tool handler entirely and use ``result``.
    - ``deny``: skip the tool handler and surface ``message`` as a tool
      error result.

    ``replace`` and ``deny`` short-circuit any remaining implementations.
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
    """Terminal outcome of one tool invocation, as seen by ``after_tool_call``.

    ``error`` is the original ``BubError`` when the invocation raised or was
    denied (kind/message/details preserved); ``result`` is unset in that
    case. Cancellation is not observed by after hooks.
    """

    run_id: str
    tool: str
    arguments: dict[str, Any]
    result: Any = None
    error: Exception | None = None
    duration_ms: int = 0
