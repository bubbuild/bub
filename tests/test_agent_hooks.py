"""Agent-loop hook semantics: chaining, short-circuit, isolation (issue #253)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pluggy
import pytest

from bub.agent_hooks import (
    LlmCallRequest,
    LlmCallResult,
    ToolCall,
    ToolCallDecision,
    ToolCallResult,
)
from bub.hook_runtime import AgentHooks, HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs, hookimpl
from bub.runtime import BubError
from bub.tools import Tool, ToolExecutor


def make_hooks(*plugins: Any) -> AgentHooks:
    pm = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
    pm.add_hookspecs(BubHookSpecs)
    for plugin in plugins:
        pm.register(plugin)
    return AgentHooks(HookRuntime(pm))


def request() -> LlmCallRequest:
    return LlmCallRequest(run_id="run-1", model="openai:gpt-x", messages=[{"role": "user", "content": "hi"}])


class TestBeforeLlmCall:
    @pytest.mark.asyncio
    async def test_chain_folds_modifications_in_order(self) -> None:
        class SwapModel:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
                return replace(request, model="anthropic:claude")

        class AppendMessage:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
                # must see SwapModel's change (registration-order chaining)
                assert request.model == "anthropic:claude"
                return replace(request, messages=[*request.messages, {"role": "user", "content": "extra"}])

        # pluggy LIFO: last registered runs first -> register AppendMessage first
        hooks = make_hooks(AppendMessage(), SwapModel())
        result, decision = await hooks.before_llm_call(request(), state={})
        assert decision is None
        assert result.model == "anthropic:claude"
        assert result.messages[-1]["content"] == "extra"

    @pytest.mark.asyncio
    async def test_none_and_bad_returns_leave_request_unchanged(self) -> None:
        class Noop:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> None:
                return None

        class BadReturn:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> Any:
                return "not-a-request"

        hooks = make_hooks(Noop(), BadReturn())
        original = request()
        assert await hooks.before_llm_call(original, state={}) == (original, None)

    @pytest.mark.asyncio
    async def test_raising_impl_is_isolated(self) -> None:
        class Boom:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
                raise RuntimeError("plugin broke")

        class After:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
                return replace(request, model="fallback:model")

        hooks = make_hooks(Boom(), After())
        result, _ = await hooks.before_llm_call(request(), state={})
        assert result.model == "fallback:model"


class TestBeforeToolCall:
    @pytest.mark.asyncio
    async def test_proceed_folds_arguments_and_later_impl_sees_them(self) -> None:
        class Rewrite:
            @hookimpl
            def before_tool_call(self, call: ToolCall, state: dict) -> ToolCallDecision:
                return ToolCallDecision.proceed(arguments={**call.arguments, "safe": True})

        class Verify:
            @hookimpl
            def before_tool_call(self, call: ToolCall, state: dict) -> None:
                assert call.arguments["safe"] is True
                return None

        hooks = make_hooks(Verify(), Rewrite())  # LIFO: Rewrite runs first
        call, decision = await hooks.before_tool_call(
            ToolCall(run_id="run-1", tool="shell", arguments={"cmd": "ls"}), state={}
        )
        assert decision.action == "proceed"
        assert call.arguments == {"cmd": "ls", "safe": True}

    @pytest.mark.asyncio
    async def test_deny_short_circuits_remaining_impls(self) -> None:
        seen: list[str] = []

        class Deny:
            @hookimpl
            def before_tool_call(self, call: ToolCall, state: dict) -> ToolCallDecision:
                return ToolCallDecision.deny("dangerous command")

        class Later:
            @hookimpl
            def before_tool_call(self, call: ToolCall, state: dict) -> None:
                seen.append(call.tool)
                return None

        hooks = make_hooks(Later(), Deny())  # LIFO: Deny runs first
        _, decision = await hooks.before_tool_call(ToolCall(run_id="run-1", tool="shell", arguments={}), state={})
        assert decision.action == "deny"
        assert decision.message == "dangerous command"
        assert seen == []


class TestToolExecutorIntegration:
    def tool(self) -> Tool:
        def handler(cmd: str) -> str:
            return f"ran:{cmd}"

        return Tool(name="shell", handler=handler, description="", parameters={})

    @pytest.mark.asyncio
    async def test_deny_surfaces_tool_error_result(self) -> None:
        class Deny:
            @hookimpl
            def before_tool_call(self, call: ToolCall, state: dict) -> ToolCallDecision:
                return ToolCallDecision.deny("blocked by policy")

        executor = ToolExecutor(hooks=make_hooks(Deny()))
        execution = await executor.execute_async([(self.tool(), {"cmd": "rm -rf /"})])
        assert execution.error is not None
        assert "blocked by policy" in execution.error.message
        assert execution.tool_results[0]["message"] == "blocked by policy"

    @pytest.mark.asyncio
    async def test_replace_skips_handler(self) -> None:
        class Replace:
            @hookimpl
            def before_tool_call(self, call: ToolCall, state: dict) -> ToolCallDecision:
                return ToolCallDecision.replace("cached result")

        executor = ToolExecutor(hooks=make_hooks(Replace()))
        execution = await executor.execute_async([(self.tool(), {"cmd": "ls"})])
        assert execution.error is None
        assert execution.tool_results == ["cached result"]

    @pytest.mark.asyncio
    async def test_after_tool_call_observes_success_and_error(self) -> None:
        observed: list[ToolCallResult] = []

        class Observe:
            @hookimpl
            def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict) -> None:
                observed.append(result)

        def failing(cmd: str) -> str:
            raise ValueError("nope")

        executor = ToolExecutor(hooks=make_hooks(Observe()))
        await executor.execute_async([(self.tool(), {"cmd": "ls"})])
        await executor.execute_async([(Tool(name="bad", handler=failing, description="", parameters={}), {"cmd": "x"})])
        assert observed[0].result == "ran:ls"
        assert observed[0].error is None
        assert observed[1].error is not None
        assert "bad" in observed[1].tool

    @pytest.mark.asyncio
    async def test_modified_arguments_reach_handler(self) -> None:
        class Rewrite:
            @hookimpl
            def before_tool_call(self, call: ToolCall, state: dict) -> ToolCallDecision:
                return ToolCallDecision.proceed(arguments={"cmd": "safe-ls"})

        executor = ToolExecutor(hooks=make_hooks(Rewrite()))
        execution = await executor.execute_async([(self.tool(), {"cmd": "rm"})])
        assert execution.tool_results == ["ran:safe-ls"]

    @pytest.mark.asyncio
    async def test_no_hooks_keeps_current_behavior(self) -> None:
        execution = await ToolExecutor().execute_async([(self.tool(), {"cmd": "ls"})])
        assert execution.tool_results == ["ran:ls"]


class TestAfterLlmCall:
    @pytest.mark.asyncio
    async def test_observe_only_and_isolated(self) -> None:
        observed: list[LlmCallResult] = []

        class Boom:
            @hookimpl
            def after_llm_call(self, request: LlmCallRequest, result: LlmCallResult, state: dict) -> None:
                raise RuntimeError("metrics plugin broke")

        class Observe:
            @hookimpl
            def after_llm_call(self, request: LlmCallRequest, result: LlmCallResult, state: dict) -> None:
                observed.append(result)

        hooks = make_hooks(Boom(), Observe())
        result = LlmCallResult(run_id="run-1", text="hello", usage={"total_tokens": 5}, duration_ms=12)
        await hooks.after_llm_call(request(), result, state={})
        assert observed == [result]


class TestWrapToolCall:
    def tool(self) -> Tool:
        calls = {"n": 0}

        def handler(cmd: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("transient")
            return f"ok:{cmd}:attempt{calls['n']}"

        return Tool(name="flaky", handler=handler, description="", parameters={})

    @pytest.mark.asyncio
    async def test_retry_wrapper_can_call_next_twice(self) -> None:
        class Retry:
            @hookimpl
            async def wrap_tool_call(self, call: ToolCall, call_next, state: dict) -> Any:
                try:
                    return await call_next(call)
                except BubError:
                    return await call_next(call)

        executor = ToolExecutor(hooks=make_hooks(Retry()))
        execution = await executor.execute_async([(self.tool(), {"cmd": "x"})])
        assert execution.error is None
        assert execution.tool_results == ["ok:x:attempt2"]

    @pytest.mark.asyncio
    async def test_cache_wrapper_can_skip_call_next(self) -> None:
        class Cache:
            @hookimpl
            async def wrap_tool_call(self, call: ToolCall, call_next, state: dict) -> Any:
                return "cached"

        def never(cmd: str) -> str:
            raise AssertionError("handler must not run")

        executor = ToolExecutor(hooks=make_hooks(Cache()))
        execution = await executor.execute_async([
            (Tool(name="t", handler=never, description="", parameters={}), {"cmd": "x"})
        ])
        assert execution.tool_results == ["cached"]

    @pytest.mark.asyncio
    async def test_wrappers_nest_last_registered_outermost(self) -> None:
        order: list[str] = []

        class Outer:
            @hookimpl
            async def wrap_tool_call(self, call: ToolCall, call_next, state: dict) -> Any:
                order.append("outer-in")
                value = await call_next(call)
                order.append("outer-out")
                return value

        class Inner:
            @hookimpl
            async def wrap_tool_call(self, call: ToolCall, call_next, state: dict) -> Any:
                order.append("inner-in")
                value = await call_next(call)
                order.append("inner-out")
                return value

        def handler(cmd: str) -> str:
            order.append("handler")
            return "done"

        executor = ToolExecutor(hooks=make_hooks(Inner(), Outer()))  # LIFO: Outer outermost
        await executor.execute_async([(Tool(name="t", handler=handler, description="", parameters={}), {"cmd": "x"})])
        assert order == ["outer-in", "inner-in", "handler", "inner-out", "outer-out"]


class TestBeforeLlmCallFinish:
    @pytest.mark.asyncio
    async def test_finish_decision_short_circuits(self) -> None:
        from bub.agent_hooks import LlmCallDecision

        class Limit:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallDecision:
                return LlmCallDecision.finish("call budget exhausted")

        class Later:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> None:
                raise AssertionError("must not run after finish")

        hooks = make_hooks(Later(), Limit())  # LIFO: Limit runs first
        _req, decision = await hooks.before_llm_call(request(), state={})
        assert decision is not None
        assert decision.text == "call budget exhausted"


class TestModelRunnerHookIntegration:
    """Regression tests for PR #255 review findings (effective request, exactly-once)."""

    def _runner_and_tape(self, hooks: AgentHooks, captured: dict):
        import bub
        from bub.builtin.model_runner import ModelRunner
        from bub.builtin.settings import AgentSettings
        from bub.builtin.tape import Tape
        from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeContext

        class FakeRunner(ModelRunner):
            async def completion_response(self, *, model, messages, tools, max_tokens=None):
                captured.update(model=model, max_tokens=max_tokens)

                async def chunks():
                    return
                    yield  # pragma: no cover

                return chunks()

        settings = AgentSettings.model_construct(model="openai:orig", max_tokens=100, model_timeout_seconds=None)
        runner = FakeRunner(settings, hooks=hooks)
        store = AsyncTapeStoreAdapter(InMemoryTapeStore())
        tape = Tape(bub.home / "tapes", store, TapeContext(anchor=None)).scoped("t1")
        return runner, tape

    @pytest.mark.asyncio
    async def test_rewritten_model_and_max_tokens_reach_provider_and_tape(self) -> None:
        class Reroute:
            @hookimpl
            def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
                return replace(request, model="anthropic:new", max_tokens=42)

        captured: dict = {}
        runner, tape = self._runner_and_tape(make_hooks(Reroute()), captured)
        events = runner.run(tape=tape, model="openai:orig", tools=[], system_prompt=None, prompt="hi")
        async for _ in events:
            pass
        assert captured == {"model": "anthropic:new", "max_tokens": 42}
        entries = list(await tape.store.fetch_all(tape.query().kinds("event")))
        run_events = [e for e in entries if e.payload.get("name") == "run"]
        assert run_events[-1].payload["data"]["model"] == "anthropic:new"

    @pytest.mark.asyncio
    async def test_after_llm_call_fires_exactly_once_on_early_close(self) -> None:
        observed: list[LlmCallResult] = []

        class Observe:
            @hookimpl
            def after_llm_call(self, request: LlmCallRequest, result: LlmCallResult, state: dict) -> None:
                observed.append(result)

        captured: dict = {}
        runner, tape = self._runner_and_tape(make_hooks(Observe()), captured)

        from bub.builtin.model_runner import ModelRunner  # noqa: F401

        async def fake_events(completion, state, output):
            from bub.runtime import StreamEvent

            yield StreamEvent("text", {"delta": "a"})
            yield StreamEvent("text", {"delta": "b"})

        runner._completion_events = fake_events  # type: ignore[method-assign]
        events = runner.run(tape=tape, model="openai:orig", tools=[], system_prompt=None, prompt="hi")
        iterator = events.__aiter__()
        await iterator.__anext__()
        await iterator.aclose()
        assert len(observed) == 1
        assert observed[0].error is not None  # closed before completion = abnormal terminal

    @pytest.mark.asyncio
    async def test_after_llm_call_fires_exactly_once_on_success(self) -> None:
        observed: list[LlmCallResult] = []

        class Observe:
            @hookimpl
            def after_llm_call(self, request: LlmCallRequest, result: LlmCallResult, state: dict) -> None:
                observed.append(result)

        captured: dict = {}
        runner, tape = self._runner_and_tape(make_hooks(Observe()), captured)
        events = runner.run(tape=tape, model="openai:orig", tools=[], system_prompt=None, prompt="hi")
        async for _ in events:
            pass
        assert len(observed) == 1
        assert observed[0].error is None


class TestWrapperEffectiveCall:
    @pytest.mark.asyncio
    async def test_after_tool_call_observes_wrapper_rewritten_arguments(self) -> None:
        observed: list[ToolCallResult] = []

        class Rewrite:
            @hookimpl
            async def wrap_tool_call(self, call: ToolCall, call_next, state: dict) -> Any:
                return await call_next(replace(call, arguments={"cmd": "rewritten"}))

        class Observe:
            @hookimpl
            def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict) -> None:
                observed.append(result)

        def handler(cmd: str) -> str:
            return f"ran:{cmd}"

        executor = ToolExecutor(hooks=make_hooks(Observe(), Rewrite()))
        execution = await executor.execute_async([
            (Tool(name="t", handler=handler, description="", parameters={}), {"cmd": "original"})
        ])
        assert execution.tool_results == ["ran:rewritten"]
        assert observed[0].arguments == {"cmd": "rewritten"}
        assert observed[0].result == "ran:rewritten"

    @pytest.mark.asyncio
    async def test_after_tool_call_uses_prewrap_call_when_next_never_invoked(self) -> None:
        observed: list[ToolCallResult] = []

        class Cache:
            @hookimpl
            async def wrap_tool_call(self, call: ToolCall, call_next, state: dict) -> Any:
                return "cached"

        class Observe:
            @hookimpl
            def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict) -> None:
                observed.append(result)

        def handler(cmd: str) -> str:
            raise AssertionError("must not run")

        executor = ToolExecutor(hooks=make_hooks(Observe(), Cache()))
        await executor.execute_async([
            (Tool(name="t", handler=handler, description="", parameters={}), {"cmd": "original"})
        ])
        assert observed[0].arguments == {"cmd": "original"}
        assert observed[0].result == "cached"
