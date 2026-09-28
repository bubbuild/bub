from __future__ import annotations

from pathlib import Path

import pytest
from model_fixtures import install_provider, reply
from republic import Message, Request, Response, TextPart, ToolCallPart, ToolResultPart, UnsupportedRequestError
from republic_fixtures import Body, Transport, sdk_transport, settings, tape_at, wire

from bub.builtin.context import default_tape_context
from bub.builtin.model_provider import tool_invocation
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings
from bub.errors import BubError, ErrorKind
from bub.store import FileTapeStore
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, Tape
from bub.tools import Tool, ToolExecutor


@pytest.mark.parametrize("provider", ["gemini", "vertexai", "azure", "acme"])
def test_unsupported_provider_is_a_configuration_error(provider: str) -> None:
    with pytest.raises(BubError) as exc:
        AgentSettings.model_construct().model_candidates(f"{provider}:model")
    assert exc.value.kind == ErrorKind.CONFIG


@pytest.mark.asyncio
async def test_unknown_tool_placeholder_surfaces_error_without_hooks() -> None:
    invocation = tool_invocation(ToolCallPart(tool_call_id="call-1", tool_name="missing_tool", tool_args="{}"), {})
    execution = await ToolExecutor().execute_async([invocation])
    assert execution.error is not None
    assert "missing_tool" in execution.error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", "Check byte equality.\nOnly exact equality counts."])
@pytest.mark.parametrize("continuation_prompt", ["Continue.", "", None])
async def test_tool_call_text_survives_into_next_request_after_tape_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str | None,
    continuation_prompt: str | None,
) -> None:
    calls = [
        ToolCallPart(tool_call_id=f"call-{name}", tool_name=name, tool_args="{}") for name in ("inspect", "compare")
    ]
    requests: list[Request] = []

    async def complete(request: Request) -> Response:
        requests.append(request)
        return reply(content or "", calls) if len(requests) == 1 else reply()

    runner = ModelRunner(AgentSettings.model_construct(model="openai:test-model", model_timeout_seconds=None))
    install_provider(monkeypatch, runner, complete)
    tools = [Tool(name="inspect", handler=lambda: "files found"), Tool(name="compare", handler=lambda: "bytes differ")]
    root = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped("test-tape")
    async with root.fork_tape() as tape:
        await tape.ensure_bootstrap_anchor()
        output = [
            event
            async for event in runner.run(
                tape=tape, model="openai:test-model", tools=tools, system_prompt=None, prompt="Compare the outputs."
            )
        ]
    reloaded = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped(
        "test-tape"
    )
    async for _ in runner.run(
        tape=reloaded, model="openai:test-model", tools=tools, system_prompt=None, prompt=continuation_prompt
    ):
        pass
    assert "".join(event.data["delta"] for event in output if event.kind == "text") == (content or "")
    expected = [
        Message(role="user", parts=[TextPart(text="Compare the outputs.")]),
        Message(role="assistant", parts=[TextPart(text=content or ""), *calls]),
        *[
            Message(role="tool", parts=[ToolResultPart(tool_call_id=f"call-{name}", tool_name=name, result=result)])
            for name, result in [("inspect", "files found"), ("compare", "bytes differ")]
        ],
    ]
    if continuation_prompt is not None:
        expected.append(Message(role="user", parts=[TextPart(text=continuation_prompt)]))
    assert requests[1].messages == expected
    if continuation_prompt is None:
        persisted = await reloaded.store.fetch_all(reloaded.query().kinds("message"))
        assert [entry.payload for entry in persisted if entry.payload.get("role") == "user"] == [
            {"role": "user", "content": "Compare the outputs."}
        ]


@pytest.mark.asyncio
async def test_build_messages_keeps_steering_when_continuation_has_no_prompt(tmp_path: Path) -> None:
    runner = ModelRunner(AgentSettings.model_construct(model="openai:test-model"))
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), default_tape_context()).scoped("steering")
    await tape.ensure_bootstrap_anchor()
    messages, new_messages = await runner.build_messages(
        tape=tape,
        run_id="run-1",
        system_prompt=None,
        prompt=None,
        model="openai:test-model",
        steering_messages=["new user direction"],
    )
    assert messages == new_messages == [{"role": "user", "content": "new user direction"}]


@pytest.mark.parametrize("arguments", ["[]", "null", "1", "not json"])
def test_function_tool_call_rejects_non_object_arguments(arguments: str) -> None:
    with pytest.raises(BubError) as exc:
        tool_invocation(ToolCallPart(tool_call_id="call-1", tool_name="inspect", tool_args=arguments), {})
    assert exc.value.kind == ErrorKind.INVALID_INPUT


@pytest.mark.asyncio
async def test_streaming_usage_requested_and_recorded_in_tape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([10.0, 12.0])
    monkeypatch.setattr("bub.builtin.model_runner.monotonic", lambda: next(clock))
    tape = tape_at(tmp_path)
    await tape.ensure_bootstrap_anchor()
    config = settings("chat")
    transport = Transport([Body(wire("chat"))])
    with sdk_transport(transport):
        output = [
            event
            async for event in ModelRunner(config).run(
                tape=tape, model=config.model, tools=[], system_prompt=None, prompt="hello"
            )
        ]
    assert len(transport.requests) == 1
    assert transport.payload()["stream_options"] == {"include_usage": True}
    usage = next(event.data for event in output if event.kind == "usage")
    assert usage["elapsed_seconds"] == 2.0
    assert usage["usage"]["prompt_tokens"] == 10
    assert usage["usage"]["completion_tokens"] == 5
    assert usage["usage"]["total_tokens"] == 15
    runs = [
        entry for entry in await tape.store.fetch_all(tape.query().kinds("event")) if entry.payload["name"] == "run"
    ]
    assert runs[0].payload["data"]["usage"] == usage["usage"]


@pytest.mark.asyncio
async def test_anthropic_prompt_caching_is_requested(tmp_path: Path) -> None:
    tape = tape_at(tmp_path)
    await tape.ensure_bootstrap_anchor()
    config = settings("messages")
    transport = Transport([Body(wire("messages"))])
    with sdk_transport(transport):
        async for _ in ModelRunner(config).run(
            tape=tape, model=config.model, tools=[], system_prompt=None, prompt="hello"
        ):
            pass
    assert transport.payload()["cache_control"] == {"type": "ephemeral"}
    assert transport.payload()["max_tokens"] == 16384
    assert "stream_options" not in transport.payload()


@pytest.mark.asyncio
async def test_run_applies_reasoning_effort_from_tape_state(tmp_path: Path) -> None:
    tape = tape_at(tmp_path)
    await tape.ensure_bootstrap_anchor()
    tape.context.state["reasoning_effort"] = "high"
    config = settings("chat")
    transport = Transport([Body(wire("chat"))])
    with sdk_transport(transport):
        async for _ in ModelRunner(config).run(
            tape=tape, model=config.model, tools=[], system_prompt=None, prompt="hello"
        ):
            pass
    assert transport.payload()["reasoning_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["model", "messages", "stream", "max_tokens", "stream_options"])
async def test_completion_args_cannot_override_managed_fields(tmp_path: Path, option: str) -> None:
    tape = tape_at(tmp_path)
    await tape.ensure_bootstrap_anchor()
    config = settings("chat", completion_args={"provider_options": {option: False}})
    transport = Transport([])
    with sdk_transport(transport), pytest.raises(UnsupportedRequestError):
        async for _ in ModelRunner(config).run(
            tape=tape, model=config.model, tools=[], system_prompt=None, prompt="hello"
        ):
            pass
    assert transport.requests == []
