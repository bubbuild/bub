"""Hook execution runtime with per-adapter fault isolation."""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator
from typing import Any

import pluggy
from loguru import logger
from republic import AsyncStreamEvents, StreamEvent, StreamState

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

    async def run_model_stream(
        self, prompt: str | list[dict], session_id: str, state: dict[str, Any]
    ) -> AsyncStreamEvents | None:
        """Run the first `run_model_stream` hook found and fallback to `run_model` hook."""
        for _, plugin in reversed(self._plugin_manager.list_name_plugin()):
            if hasattr(plugin, "run_model_stream"):
                return await self.call_first("run_model_stream", prompt=prompt, session_id=session_id, state=state)
            elif hasattr(plugin, "run_model"):

                async def iterator() -> AsyncGenerator[StreamEvent, None]:
                    result = await self.call_first("run_model", prompt=prompt, session_id=session_id, state=state)
                    yield StreamEvent("text", {"delta": result})

                return AsyncStreamEvents(iterator(), state=StreamState())
        return None


_SKIP_VALUE = object()
