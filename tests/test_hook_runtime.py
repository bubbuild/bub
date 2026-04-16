import pluggy
import pytest
from republic import AsyncStreamEvents, StreamEvent

from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs, hookimpl


def _runtime_with_plugins(*plugins: tuple[str, object]) -> HookRuntime:
    manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
    manager.add_hookspecs(BubHookSpecs)
    for name, plugin in plugins:
        manager.register(plugin, name=name)
    return HookRuntime(manager)


@pytest.mark.asyncio
async def test_call_first_respects_priority_and_returns_first_non_none() -> None:
    called: list[str] = []

    class LowPriority:
        @hookimpl
        def resolve_session(self, message):
            called.append("low")
            return "low"

    class MidPriority:
        @hookimpl
        def resolve_session(self, message):
            called.append("mid")
            return "mid"

    class HighPriorityReturnsNone:
        @hookimpl
        def resolve_session(self, message):
            called.append("high")
            return None

    runtime = _runtime_with_plugins(
        ("low", LowPriority()),
        ("mid", MidPriority()),
        ("high", HighPriorityReturnsNone()),
    )

    result = await runtime.call_first("resolve_session", message={"session_id": "x"}, ignored="value")
    assert result == "mid"
    assert called == ["high", "mid"]


def test_call_many_sync_skips_async_impl() -> None:
    class _AwaitableValue:
        def __await__(self):
            yield from ()
            return "async"

    class AsyncPrompt:
        @hookimpl
        def system_prompt(self, prompt, state):
            return _AwaitableValue()

    class SyncPrompt:
        @hookimpl
        def system_prompt(self, prompt, state):
            return "sync"

    runtime = _runtime_with_plugins(
        ("sync", SyncPrompt()),
        ("async", AsyncPrompt()),
    )

    assert runtime.call_many_sync("system_prompt", prompt="hello", state={}) == ["sync"]


@pytest.mark.asyncio
async def test_notify_error_swallows_observer_failures() -> None:
    observed: list[str] = []

    class RaisingObserver:
        @hookimpl
        async def on_error(self, stage, error, message):
            raise RuntimeError("boom")

    class RecordingObserver:
        @hookimpl
        async def on_error(self, stage, error, message):
            observed.append(stage)

    runtime = _runtime_with_plugins(
        ("raise", RaisingObserver()),
        ("record", RecordingObserver()),
    )

    await runtime.notify_error(stage="turn", error=ValueError("bad"), message={"content": "x"})
    assert observed == ["turn"]


def test_hook_report_lists_registered_implementations() -> None:
    class SessionPlugin:
        @hookimpl
        def resolve_session(self, message):
            return "session"

    runtime = _runtime_with_plugins(("session", SessionPlugin()))
    report = runtime.hook_report()

    assert "resolve_session" in report
    assert report["resolve_session"] == ["session"]


@pytest.mark.asyncio
async def test_run_model_uses_streaming_hook_when_plain_hook_absent() -> None:
    class StreamPlugin:
        @hookimpl
        async def run_model_stream(self, prompt, session_id, state):
            async def iterator():
                yield StreamEvent("text", {"delta": "stream"})
                yield StreamEvent("text", {"delta": "ed"})

            return AsyncStreamEvents(iterator())

    runtime = _runtime_with_plugins(("stream", StreamPlugin()))

    result = await runtime.run_model(prompt="hello", session_id="s", state={})

    assert result == "streamed"


@pytest.mark.asyncio
async def test_run_model_stream_falls_back_to_plain_hook() -> None:
    class PlainPlugin:
        @hookimpl
        async def run_model(self, prompt, session_id, state):
            return "plain"

    runtime = _runtime_with_plugins(("plain", PlainPlugin()))

    stream = await runtime.run_model_stream(prompt="hello", session_id="s", state={})

    assert stream is not None
    events = [event async for event in stream]
    assert [(event.kind, event.data) for event in events] == [("text", {"delta": "plain"})]
