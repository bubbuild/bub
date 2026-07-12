"""Hook execution runtime with per-adapter fault isolation."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import Any

import pluggy
from loguru import logger

from bub.runtime import (
    AsyncStreamEvents,
    LlmCallDecision,
    LlmCallRequest,
    LlmCallResult,
    StreamEvent,
    StreamState,
    ToolCall,
    ToolCallDecision,
    ToolCallResult,
)
from bub.types import Envelope


class HookRuntime:
    """Safe wrapper around pluggy hook execution."""

    def __init__(self, plugin_manager: pluggy.PluginManager) -> None:
        self._plugin_manager = plugin_manager

    async def call_first(self, hook_name: str, **kwargs: Any) -> Any:
        """Run hook implementations in precedence order and return first non-None value."""

        for impl in self._iter_hookimpls(hook_name):
            call_kwargs = self._kwargs_for_impl(impl, kwargs)
            value = await self._invoke_impl_async(
                hook_name=hook_name, impl=impl, call_kwargs=call_kwargs, kwargs=kwargs
            )
            if value is _SKIP_VALUE:
                continue
            if value is not None:
                return value
        return None

    async def call_many(self, hook_name: str, **kwargs: Any) -> list[Any]:
        """Run all implementations and collect successful return values."""

        results: list[Any] = []
        for impl in self._iter_hookimpls(hook_name):
            call_kwargs = self._kwargs_for_impl(impl, kwargs)
            value = await self._invoke_impl_async(
                hook_name=hook_name, impl=impl, call_kwargs=call_kwargs, kwargs=kwargs
            )
            if value is _SKIP_VALUE:
                continue
            results.append(value)
        return results

    def call_first_sync(self, hook_name: str, **kwargs: Any) -> Any:
        """Synchronous variant of call_first for bootstrap hooks."""

        for impl in self._iter_hookimpls(hook_name):
            call_kwargs = self._kwargs_for_impl(impl, kwargs)
            value = self._invoke_impl_sync(hook_name=hook_name, impl=impl, call_kwargs=call_kwargs, kwargs=kwargs)
            if value is _SKIP_VALUE:
                continue
            if value is not None:
                return value
        return None

    def call_many_sync(self, hook_name: str, **kwargs: Any) -> list[Any]:
        """Synchronous variant of call_many for bootstrap hooks."""

        results: list[Any] = []
        for impl in self._iter_hookimpls(hook_name):
            call_kwargs = self._kwargs_for_impl(impl, kwargs)
            value = self._invoke_impl_sync(hook_name=hook_name, impl=impl, call_kwargs=call_kwargs, kwargs=kwargs)
            if value is _SKIP_VALUE:
                continue
            results.append(value)
        return results

    async def notify_error(self, *, stage: str, error: Exception, message: Envelope | None) -> None:
        """Call on_error hooks, swallowing observer failures."""

        for impl in self._iter_hookimpls("on_error"):
            call_kwargs = self._kwargs_for_impl(impl, {"stage": stage, "error": error, "message": message})
            try:
                value = impl.function(**call_kwargs)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                logger.opt(exception=True).warning(
                    "hook.on_error_failed stage={} adapter={}",
                    stage,
                    impl.plugin_name or "<unknown>",
                )

    def notify_error_sync(self, *, stage: str, error: Exception, message: Envelope | None) -> None:
        """Synchronous on_error dispatch for bootstrap paths."""

        for impl in self._iter_hookimpls("on_error"):
            call_kwargs = self._kwargs_for_impl(impl, {"stage": stage, "error": error, "message": message})
            try:
                value = impl.function(**call_kwargs)
            except Exception:
                logger.opt(exception=True).warning(
                    "hook.on_error_failed stage={} adapter={}",
                    stage,
                    impl.plugin_name or "<unknown>",
                )
                continue
            if inspect.isawaitable(value):
                logger.warning(
                    "hook.async_not_supported hook=on_error adapter={}",
                    impl.plugin_name or "<unknown>",
                )

    def hook_report(self) -> dict[str, list[str]]:
        """Build a hook->adapters mapping for diagnostics."""

        report: dict[str, list[str]] = {}
        for hook_name, hook_caller in sorted(self._plugin_manager.hook.__dict__.items()):
            if hook_name.startswith("_") or not hasattr(hook_caller, "get_hookimpls"):
                continue
            adapter_names = [impl.plugin_name for impl in hook_caller.get_hookimpls()]
            if adapter_names:
                report[hook_name] = adapter_names
        return report

    async def _invoke_impl_async(
        self,
        *,
        hook_name: str,
        impl: Any,
        call_kwargs: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> Any:
        value = impl.function(**call_kwargs)
        if inspect.isawaitable(value):
            value = await value
        return value

    def _invoke_impl_sync(
        self,
        *,
        hook_name: str,
        impl: Any,
        call_kwargs: dict[str, Any],
        kwargs: dict[str, Any],
    ) -> Any:
        value = impl.function(**call_kwargs)
        if inspect.isawaitable(value):
            logger.warning(
                "hook.async_not_supported hook={} adapter={}",
                hook_name,
                impl.plugin_name or "<unknown>",
            )
            return _SKIP_VALUE
        return value

    def _iter_hookimpls(self, hook_name: str) -> list[Any]:
        hook = getattr(self._plugin_manager.hook, hook_name, None)
        if hook is None or not hasattr(hook, "get_hookimpls"):
            return []
        return list(reversed(hook.get_hookimpls()))

    @staticmethod
    def _kwargs_for_impl(impl: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {name: kwargs[name] for name in impl.argnames if name in kwargs}

    async def run_model(self, prompt: str | list[dict], session_id: str, state: dict[str, Any]) -> str | None:
        """Run the first `run_model` hook found and return its result."""
        for _, plugin in reversed(self._plugin_manager.list_name_plugin()):
            if hasattr(plugin, "run_model"):
                output = await self.call_first("run_model", prompt=prompt, session_id=session_id, state=state)
                if output is None or isinstance(output, str):
                    return output
                raise TypeError("hook.run_model must return str or None")
            elif hasattr(plugin, "run_model_stream"):
                stream = await self.call_first("run_model_stream", prompt=prompt, session_id=session_id, state=state)
                text = ""
                async for event in stream:
                    if event.kind == "text":
                        text += str(event.data.get("delta", ""))
                return text
        return None

    async def run_model_stream(
        self, prompt: str | list[dict], session_id: str, state: dict[str, Any]
    ) -> AsyncStreamEvents | None:
        """Run the first `run_model_stream` hook found and fallback to `run_model` hook."""
        for _, plugin in reversed(self._plugin_manager.list_name_plugin()):
            if hasattr(plugin, "run_model_stream"):
                stream = await self.call_first("run_model_stream", prompt=prompt, session_id=session_id, state=state)
                if stream is None or isinstance(stream, AsyncStreamEvents):
                    return stream
                raise TypeError("hook.run_model_stream must return AsyncStreamEvents or None")
            elif hasattr(plugin, "run_model"):

                async def iterator() -> AsyncGenerator[StreamEvent, None]:
                    result = await self.call_first("run_model", prompt=prompt, session_id=session_id, state=state)
                    yield StreamEvent("text", {"delta": result})

                return AsyncStreamEvents(iterator(), state=StreamState())
        return None


class AgentHooks:
    """Narrow facade for agent-loop interception hooks (issue #253).

    Unlike ``call_first``/``call_many``, every implementation call here is
    fault-isolated: a raising plugin is logged and skipped, never fatal to
    the turn. Blocking a tool call is only possible through a returned
    :class:`~bub.runtime.ToolCallDecision` (``deny``/``replace``) — an
    exception inside ``before_tool_call`` is treated as a broken plugin,
    not as a veto.
    """

    def __init__(self, runtime: HookRuntime) -> None:
        self._runtime = runtime

    async def before_llm_call(
        self, request: LlmCallRequest, state: dict[str, Any]
    ) -> tuple[LlmCallRequest, LlmCallDecision | None]:
        """Chain ``before_llm_call`` impls; each sees the previous impl's request.

        The first ``LlmCallDecision`` (``finish``) short-circuits remaining
        implementations and the provider call itself.
        """

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

    async def after_llm_call(self, request: LlmCallRequest, result: LlmCallResult, state: dict[str, Any]) -> None:
        """Observe-only; return values are ignored."""

        await self._safe_calls("after_llm_call", lambda: {"request": request, "result": result, "state": state})

    async def before_tool_call(self, call: ToolCall, state: dict[str, Any]) -> tuple[ToolCall, ToolCallDecision]:
        """Chain ``before_tool_call`` impls.

        ``proceed`` decisions fold argument changes into the call visible to
        later impls; the first ``replace``/``deny`` decision short-circuits.
        """

        from dataclasses import replace as dc_replace

        for impl in self._runtime._iter_hookimpls("before_tool_call"):
            value = await self._safe_call_one("before_tool_call", impl, {"call": call, "state": state})
            if value is None or value is _SKIP_VALUE:
                continue
            if not isinstance(value, ToolCallDecision):
                self._warn_bad_return("before_tool_call", impl, value, expected="ToolCallDecision | None")
                continue
            if value.action == "proceed":
                if value.arguments is not None:
                    call = dc_replace(call, arguments=dict(value.arguments))
                continue
            return call, value
        return call, ToolCallDecision.proceed()

    async def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict[str, Any]) -> None:
        """Observe-only; return values are ignored."""

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


_SKIP_VALUE = object()
