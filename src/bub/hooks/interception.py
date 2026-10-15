"""Pluggy adapter turning agent-loop events into interception hook calls."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from loguru import logger

from bub.core.events import (
    AfterLlmCall,
    AfterToolCall,
    BeforeLlmCall,
    BeforeToolCall,
    EventBus,
    LlmCallDecision,
    LlmCallRequest,
    LlmCallResult,
    ToolCall,
    ToolCallDecision,
    ToolCallResult,
)
from bub.hooks.runtime import _SKIP_VALUE, HookRuntime
from bub.turn import TurnState

__all__ = [
    "LlmCallDecision",
    "LlmCallRequest",
    "LlmCallResult",
    "PluginAgentHooks",
    "ToolCall",
    "ToolCallDecision",
    "ToolCallResult",
]


class PluginAgentHooks:
    """Fault-isolated adapter running the framework's pluggy interception hooks for agent events.

    Blocking a tool call is only possible through a returned
    :class:`ToolCallDecision`; an exception inside ``before_tool_call`` is
    treated as a broken plugin, not as a veto.
    """

    def __init__(self, runtime: HookRuntime) -> None:
        self._runtime = runtime

    def subscribe(self, events: EventBus | None = None) -> EventBus:
        """Handle an agent's ``Before/After*Call`` events with the plugin hooks; return the bus."""
        events = EventBus() if events is None else events
        events.on(BeforeLlmCall, self._on_before_llm_call)
        events.on(AfterLlmCall, self._on_after_llm_call)
        events.on(BeforeToolCall, self._on_before_tool_call)
        events.on(AfterToolCall, self._on_after_tool_call)
        return events

    async def _on_before_llm_call(self, event: BeforeLlmCall) -> None:
        event.request, event.decision = await self.before_llm_call(event.request, event.state)

    async def _on_after_llm_call(self, event: AfterLlmCall) -> None:
        await self.after_llm_call(event.request, event.result, event.state)

    async def _on_before_tool_call(self, event: BeforeToolCall) -> None:
        event.call, event.decision = await self.before_tool_call(event.call, event.state)

    async def _on_after_tool_call(self, event: AfterToolCall) -> None:
        await self.after_tool_call(event.call, event.result, event.state)

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
        """Notify every implementation with one shared, mutable outcome."""

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
