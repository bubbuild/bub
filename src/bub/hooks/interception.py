"""Contracts and execution semantics for agent-loop interception hooks."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from loguru import logger

from bub.hooks.runtime import _SKIP_VALUE, HookRuntime
from bub.turn import TurnState


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


class AgentHooks:
    """Fault-isolated executor for agent-loop interception hooks.

    Blocking a tool call is only possible through a returned
    :class:`ToolCallDecision`; an exception inside ``before_tool_call`` is
    treated as a broken plugin, not as a veto.
    """

    def __init__(self, runtime: HookRuntime) -> None:
        self._runtime = runtime

    async def before_llm_call(
        self, request: LlmCallRequest, state: TurnState
    ) -> tuple[LlmCallRequest, LlmCallDecision | None]:
        """Chain hooks so each implementation sees the previous request."""

        for impl in self._runtime._iter_hookimpls("before_llm_call"):
            value = await self._safe_call_one("before_llm_call", impl, {"request": request, "state": state})
            if value is None or value is _SKIP_VALUE:
                continue
            if isinstance(value, LlmCallRequest):
                request = value
            elif isinstance(value, LlmCallDecision):
                return request, value
            else:
                self._warn_bad_return(
                    "before_llm_call", impl, value, expected="LlmCallRequest | LlmCallDecision | None"
                )
        return request, None

    async def after_llm_call(
        self,
        request: LlmCallRequest,
        result: LlmCallResult,
        state: TurnState,
    ) -> None:
        """Notify every observer; return values are ignored."""

        await self._safe_calls("after_llm_call", lambda: {"request": request, "result": result, "state": state})

    async def before_tool_call(self, call: ToolCall, state: TurnState) -> tuple[ToolCall, ToolCallDecision]:
        """Chain argument changes and stop at the first replace or deny."""

        for impl in self._runtime._iter_hookimpls("before_tool_call"):
            value = await self._safe_call_one("before_tool_call", impl, {"call": call, "state": state})
            if value is None or value is _SKIP_VALUE:
                continue
            if not isinstance(value, ToolCallDecision):
                self._warn_bad_return("before_tool_call", impl, value, expected="ToolCallDecision | None")
                continue
            if value.action == "proceed":
                if value.arguments is not None:
                    call = replace(call, arguments=dict(value.arguments))
                continue
            return call, value
        return call, ToolCallDecision.proceed()

    async def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: TurnState) -> None:
        """Notify every observer; return values are ignored."""

        await self._safe_calls("after_tool_call", lambda: {"call": call, "state": state, "result": result})

    async def _safe_calls(self, hook_name: str, kwargs_factory: Any) -> list[tuple[Any, Any]]:
        outcomes: list[tuple[Any, Any]] = []
        for impl in self._runtime._iter_hookimpls(hook_name):
            value = await self._safe_call_one(hook_name, impl, kwargs_factory())
            if value is _SKIP_VALUE:
                continue
            outcomes.append((impl, value))
        return outcomes

    async def _safe_call_one(self, hook_name: str, impl: Any, kwargs: dict[str, Any]) -> Any:
        call_kwargs = self._runtime._kwargs_for_impl(impl, kwargs)
        try:
            return await self._runtime._invoke_impl_async(
                hook_name=hook_name, impl=impl, call_kwargs=call_kwargs, kwargs=kwargs
            )
        except Exception:
            logger.opt(exception=True).warning(
                "hook.agent_hook_failed hook={} adapter={}",
                hook_name,
                impl.plugin_name or "<unknown>",
            )
            return _SKIP_VALUE

    @staticmethod
    def _warn_bad_return(hook_name: str, impl: Any, value: Any, *, expected: str) -> None:
        logger.warning(
            "hook.agent_hook_bad_return hook={} adapter={} got={} expected={}",
            hook_name,
            impl.plugin_name or "<unknown>",
            type(value).__name__,
            expected,
        )
