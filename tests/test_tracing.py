from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from pathlib import Path
from typing import Any

import pytest
from any_llm.constants import LLMProvider
from any_llm.types.completion import ChatCompletion, ChatCompletionChunk

from bub import tracing
from bub.builtin.agent import Agent
from bub.builtin.context import default_tape_context
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings, ModelCandidate
from bub.framework import BubFramework
from bub.hooks import hookimpl
from bub.hooks.interception import LlmCallDecision, ToolCallDecision, ToolCallResult
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub.tape import Tape
from bub.tools import REGISTRY, Tool, ToolContext, ToolExecutor
from bub.utils import workspace_from_state


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = sdk.TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing.otel, "get_tracer", provider.get_tracer)
    yield exporter
    provider.shutdown()


@pytest.fixture
def agent(tmp_path: Path) -> Agent:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    agent = Agent(framework)
    agent.settings = AgentSettings.model_construct(model="openai:test", api_key="unused", api_base=None)
    agent.model_runner = ModelRunner(agent.settings, hooks=framework.get_agent_hooks())
    agent.__dict__["tape"] = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), default_tape_context())
    return agent


def completion(text: str = "done", calls: list[dict[str, Any]] | None = None) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "response-1",
        "object": "chat.completion",
        "created": 0,
        "model": "actual-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": text, "tool_calls": calls},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    })


def call(name: str, call_id: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {})}}


@pytest.mark.asyncio
async def test_agent_trajectory_has_parallel_tools_messages_and_tape_links(
    spans: Any,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = 0
    both_started = asyncio.Event()

    async def handler() -> dict[str, str]:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        return {"value": "result"}

    monkeypatch.setitem(REGISTRY, "trace_tool", Tool(name="trace_tool", handler=handler))
    replies = iter([completion("checking", [call("trace_tool", "call-1"), call("trace_tool", "call-2")]), completion()])

    async def respond(**kwargs: Any) -> ChatCompletion:
        return next(replies)

    monkeypatch.setattr(agent.model_runner, "completion_response", respond)
    events = await agent.run_stream(session_id="trace-test", prompt="hello", state={}, allowed_tools=["trace_tool"])
    async for _ in events:
        assert tracing.current_span() is None
        assert not tracing.otel.get_current_span().get_span_context().is_valid

    finished = spans.get_finished_spans()
    root = next(s for s in finished if s.name == "invoke_agent bub")
    models = [s for s in finished if s.name.startswith("chat ")]
    tools = [s for s in finished if s.name.startswith("execute_tool ")]
    assert len(models) == len(tools) == 2
    assert all(s.parent.span_id == root.context.span_id for s in [*models, *tools])
    assert tools[0].start_time < tools[1].end_time and tools[1].start_time < tools[0].end_time
    assert {s.attributes["gen_ai.tool.call.id"] for s in tools} == {"call-1", "call-2"}
    assert all(s.attributes["gen_ai.response.model"] == "actual-model" for s in models)
    assert all(s.attributes["gen_ai.usage.input_tokens"] == 10 for s in models)
    assert "gen_ai.usage.input_tokens" not in root.attributes  # No double counting.
    assert root.attributes["gen_ai.conversation.id"] == "trace-test"
    assert root.attributes["openinference.span.kind"] == "AGENT"
    assert all(s.attributes["openinference.span.kind"] == "LLM" for s in models)
    assert all(s.attributes["openinference.span.kind"] == "TOOL" for s in tools)
    assert models[0].attributes["llm.token_count.prompt"] == 10
    assert models[0].attributes["llm.model_name"] == "actual-model"
    assert models[0].attributes["llm.output_messages.0.message.tool_calls.0.tool_call.id"] == "call-1"
    assert json.loads(tools[0].attributes["output.value"]) == {"value": "result"}
    messages = json.loads(root.attributes["gen_ai.output.messages"])
    assert [m["role"] for m in messages] == ["assistant", "tool", "tool", "assistant"]
    assert messages[-1]["parts"] == [{"type": "text", "content": "done"}]
    assert any(e.name == "bub.loop.step" for e in root.events)
    entries = await agent.tape.store.fetch_all(agent.tape.session_tape("trace-test", workspace_from_state({})).query())
    assert any(e.meta.get("trace_id") == f"{root.context.trace_id:032x}" for e in entries)


@pytest.mark.asyncio
async def test_subagent_is_nested_under_its_tool(spans: Any, agent: Agent, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = iter([
        completion("", [call("subagent", "child", {"prompt": "child task"})]),
        completion("child done"),
        completion(),
    ])

    async def respond(**kwargs: Any) -> ChatCompletion:
        return next(replies)

    monkeypatch.setattr(agent.model_runner, "completion_response", respond)
    events = await agent.run_stream(
        session_id="parent", prompt="delegate", state={"_runtime_agent": agent}, allowed_tools=["subagent"]
    )
    async for _ in events:
        pass
    finished = spans.get_finished_spans()
    roots = [s for s in finished if s.name == "invoke_agent bub"]
    tool = next(s for s in finished if s.name == "execute_tool subagent")
    assert len(roots) == 2
    child = next(s for s in roots if s.parent is not None)
    parent = next(s for s in roots if s.parent is None)
    assert child.parent.span_id == tool.context.span_id
    assert tool.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_close_and_cancel_finish_spans_and_provider(
    spans: Any,
    agent: Agent,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    closed = asyncio.Event()
    waiting = asyncio.Event()

    async def chunks() -> AsyncIterator[ChatCompletionChunk]:
        try:
            yield ChatCompletionChunk.model_validate({
                "id": "chunk-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "actual",
                "choices": [{"index": 0, "delta": {"content": "first"}}],
            })
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def respond(**kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
        return chunks()

    monkeypatch.setattr(agent.model_runner, "completion_response", respond)
    events = await agent.run_stream(session_id="cancel-test", prompt="hello", state={}, allowed_tools=[])
    assert (await anext(events)).kind == "text"
    assert tracing.current_span() is None
    assert len(spans.get_finished_spans()) == 0
    if cancel:
        task = asyncio.create_task(anext(events))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await events.aclose()
    assert closed.is_set()
    assert len(spans.get_finished_spans()) == 2
    assert all(s.attributes.get("bub.cancelled") for s in spans.get_finished_spans())
    assert tracing.current_span() is None
    assert not tracing.otel.get_current_span().get_span_context().is_valid
    await events.aclose()
    assert len(spans.get_finished_spans()) == 2


@pytest.mark.asyncio
async def test_close_before_first_iteration_releases_tape(spans: Any, agent: Agent) -> None:
    events = await agent.run_stream(session_id="unused", prompt="hello", state={}, allowed_tools=[])
    await events.aclose()
    assert len(spans.get_finished_spans()) == 1
    # Fork contents were merged by the close callback even though the generator never started.
    entries = await agent.tape.store.fetch_all(agent.tape.session_tape("unused", workspace_from_state({})).query())
    assert any(e.payload.get("name") == "loop.start" for e in entries)


@pytest.mark.asyncio
async def test_setup_failure_releases_tape_and_finishes_agent_span(
    spans: Any, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_setup(*, tape: Tape, **kwargs: Any) -> AsyncStreamEvents:
        await tape.append_event("setup.started", {})
        raise ValueError("setup failed")

    monkeypatch.setattr(agent, "_agent_loop", fail_setup)
    with pytest.raises(ValueError, match="setup failed"):
        await agent.run_stream(session_id="setup-failure", prompt="hello", state={})
    (span,) = spans.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["error.type"] == "ValueError"
    entries = await agent.tape.store.fetch_all(
        agent.tape.session_tape("setup-failure", workspace_from_state({})).query()
    )
    assert any(e.payload.get("name") == "setup.started" for e in entries)
    assert tracing.current_span() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", [False, True])
async def test_cleanup_failure_finishes_span_without_leaking_context(spans: Any, consume: bool) -> None:
    async def source() -> AsyncIterator[StreamEvent]:
        yield StreamEvent("text", {"delta": "ok"})

    async def cleanup() -> None:
        raise RuntimeError("cleanup failed")

    events = AsyncStreamEvents(source(), span=tracing.Span("cleanup"), on_close=cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        if consume:
            async for _ in events:
                pass
        else:
            await events.aclose()
    await events.aclose()
    (span,) = spans.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["error.type"] == "RuntimeError"
    assert bool(span.attributes.get("bub.cancelled")) is not consume
    assert tracing.current_span() is None
    assert not tracing.otel.get_current_span().get_span_context().is_valid


@pytest.mark.asyncio
async def test_llm_short_circuit_has_no_phantom_model_span(spans: Any, agent: Agent) -> None:
    class Finish:
        @hookimpl
        def before_llm_call(self) -> LlmCallDecision:
            return LlmCallDecision.finish("stopped by policy")

    agent.framework.register_plugin(Finish())
    events = await agent.run_stream(session_id="policy", prompt="hello", state={}, allowed_tools=[])
    assert [e.data["delta"] async for e in events if e.kind == "text"] == ["stopped by policy"]
    assert [s.name for s in spans.get_finished_spans()] == ["invoke_agent bub"]


@pytest.mark.asyncio
async def test_failed_tool_records_effective_result_and_original_failure(spans: Any, agent: Agent) -> None:
    class Policy:
        @hookimpl(tryfirst=True)
        def before_tool_call(self) -> ToolCallDecision:
            return ToolCallDecision.deny("denied")

        @hookimpl
        def after_tool_call(self, result: ToolCallResult) -> None:
            result.result = "bounded failure"

    agent.framework.register_plugin(Policy())
    execution = await ToolExecutor(agent.framework.get_agent_hooks()).execute_async(
        [(Tool(name="denied", handler=lambda: pytest.fail("must not run")), {})],
        context=ToolContext(agent.tape, "run-1"),
        call_ids=["denied-1"],
    )
    assert execution.error is not None
    (span,) = spans.get_finished_spans()
    assert span.status.status_code.name == "ERROR"
    assert span.attributes["gen_ai.tool.call.result"] == "bounded failure"
    assert span.attributes["gen_ai.tool.call.id"] == "denied-1"


@pytest.mark.asyncio
async def test_provider_fallback_records_actual_model(
    spans: Any, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        SUPPORTS_COMPLETION_STREAMING = False

        async def acompletion(self, **kwargs: Any) -> ChatCompletion:
            if kwargs["model"] == "unavailable":
                raise RuntimeError("try next")
            return completion()

    candidates = [
        ModelCandidate(name=f"openai:{name}", provider=LLMProvider.OPENAI, model_id=name)
        for name in ["unavailable", "fallback"]
    ]
    monkeypatch.setattr(agent.model_runner, "iter_llm_clients", lambda model: iter((c, Client()) for c in candidates))
    events = await agent.run_stream(session_id="fallback", prompt="hello", state={}, allowed_tools=[])
    async for _ in events:
        pass
    model = next(s for s in spans.get_finished_spans() if s.name.startswith("chat "))
    assert model.attributes["gen_ai.request.model"] == "fallback"
    assert model.attributes["gen_ai.provider.name"] == "openai"
    assert any(e.name == "bub.model.attempt_failed" for e in model.events)


def test_missing_optional_dependencies_import_and_execute_as_noop() -> None:
    code = """
import asyncio
import sys
from importlib.abc import MetaPathFinder
class BlockTelemetry(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'opentelemetry', 'logfire'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, BlockTelemetry())
from bub import tracing
from bub.builtin.agent import Agent
from bub.tools import Tool, ToolExecutor
assert tracing.otel is None
async def main():
    span = tracing.Span('noop')
    assert not span.recording
    with span.activate():
        result = await ToolExecutor().execute_async([(Tool(name='echo', handler=lambda: 'ok'), {})])
        assert result.tool_results == ['ok']
    span.end()
    assert tracing.correlation() == {}
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_streaming_usage_arrives_on_last_chunk(spans: Any, agent: Agent, monkeypatch: pytest.MonkeyPatch) -> None:
    async def chunks() -> AsyncIterator[ChatCompletionChunk]:
        yield ChatCompletionChunk.model_validate({
            "id": "stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "stream-model",
            "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
        })
        yield ChatCompletionChunk.model_validate({
            "id": "stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "stream-model",
            "choices": [],
            "usage": {"prompt_tokens": 19, "completion_tokens": 3, "total_tokens": 22},
        })

    async def respond(**kwargs: Any) -> AsyncIterator[ChatCompletionChunk]:
        return chunks()

    monkeypatch.setattr(agent.model_runner, "completion_response", respond)
    events = await agent.run_stream(session_id="stream", prompt="hello", state={}, allowed_tools=[])
    assert (await anext(events)).data == {"delta": "done"}
    assert not spans.get_finished_spans()
    async for _ in events:
        pass
    model = next(s for s in spans.get_finished_spans() if s.name.startswith("chat "))
    assert model.attributes["gen_ai.usage.input_tokens"] == 19
    assert model.attributes["gen_ai.usage.output_tokens"] == 3
    assert model.attributes["gen_ai.response.finish_reasons"] == ("stop",)
    assert "bub.cancelled" not in model.attributes


@pytest.mark.asyncio
async def test_timeout_marks_model_and_agent_as_failed(
    spans: Any, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent.settings.model_timeout_seconds = 0

    async def respond(**kwargs: Any) -> ChatCompletion:
        await asyncio.Event().wait()
        return completion()

    monkeypatch.setattr(agent.model_runner, "completion_response", respond)
    events = await agent.run_stream(session_id="timeout", prompt="hello", state={}, allowed_tools=[])
    with pytest.raises(TimeoutError):
        async for _ in events:
            pass
    finished = spans.get_finished_spans()
    assert len(finished) == 2
    assert all(s.status.status_code.name == "ERROR" for s in finished)
    assert all(s.attributes["error.type"] == "TimeoutError" for s in finished)


@pytest.mark.asyncio
async def test_concurrent_sessions_have_independent_traces(
    spans: Any, agent: Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def respond(**kwargs: Any) -> ChatCompletion:
        await asyncio.sleep(0)
        return completion()

    monkeypatch.setattr(agent.model_runner, "completion_response", respond)

    async def run(session: str) -> None:
        events = await agent.run_stream(session_id=session, prompt="hello", state={}, allowed_tools=[])
        async for _ in events:
            assert tracing.current_span() is None

    await asyncio.gather(run("one"), run("two"))
    roots = [s for s in spans.get_finished_spans() if s.name == "invoke_agent bub"]
    assert len({s.context.trace_id for s in roots}) == 2
    for model in [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]:
        root = next(s for s in roots if s.context.trace_id == model.context.trace_id)
        assert model.parent.span_id == root.context.span_id
        assert model.attributes["gen_ai.conversation.id"] == root.attributes["gen_ai.conversation.id"]


@pytest.mark.asyncio
async def test_noop_stream_still_closes_when_dependencies_are_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "otel", None)
    closed = False

    async def source() -> AsyncIterator[StreamEvent]:
        nonlocal closed
        try:
            yield StreamEvent("text", {"delta": "ok"})
        finally:
            closed = True

    events = AsyncStreamEvents(source(), span=tracing.Span("noop"))
    async with aclosing(events):
        assert (await anext(events)).data == {"delta": "ok"}
    assert closed
    assert tracing.current_span() is None
