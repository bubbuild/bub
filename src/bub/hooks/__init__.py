"""Public entry points for implementing Bub hooks."""

from bub.hooks.specs import BUB_HOOK_NAMESPACE, BubHookSpecs, hookimpl, hookspec

__all__ = ["BUB_HOOK_NAMESPACE", "BubHookSpecs", "hookimpl", "hookspec"]
