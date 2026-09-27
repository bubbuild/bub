"""Optional local-wheel acceptance: real Bub lifecycle and official SDK wire."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from republic_fixtures import Body, Transport, chat, messages, responses, sdk_transport, settings, sse, tape_at, wire
from test_agent_hooks import make_hooks

from bub.builtin.model_runner import ModelRunner
from bub.errors import BubError
from bub.hooks import hookimpl
from bub.hooks.interception import LlmCallDecision, LlmCallRequest, LlmCallResult, ToolCallDecision, ToolCallResult
from bub.tools import Tool

republic = pytest.importorskip("republic", reason="explicit local Republic wheel is optional")


async def collect(
    runner: ModelRunner, tape: Any, *, tools: list[Tool] | None = None, prompt: str | None = "hello"
) -> list[Any]:
    await tape.ensure_bootstrap_anchor()
    output = runner.run(tape=tape, model=runner.settings.model, tools=tools or [], system_prompt=None, prompt=prompt)
    async with aclosing(output):
        return [item async for item in output]


@pytest.mark.parametrize("protocol", ["responses", "messages"])
def test_fresh_process_tool_tape_native_history(protocol: str, tmp_path: Path) -> None:
    script = Path(__file__).with_name("republic_process.py")
    for phase in (1, 2):
        result = subprocess.run(
            [sys.executable, str(script), protocol, str(phase), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    first, second = [json.loads((tmp_path / f"phase-{phase}.json").read_text()) for phase in (1, 2)]
    assert first["pid"] != second["pid"] and first["requests"] == second["requests"] == 1
    assert first["wheel_version"] == second["wheel_version"]
    assert "/site-packages/" in first["republic_import"]
    assert (tmp_path / "executions.log").read_text() == "2\n"
    persisted = (tmp_path / "integration.jsonl").read_text()
    assert "opaque-reasoning" in persisted if protocol == "responses" else "sig-opaque" in persisted
    assert "call-original" in persisted and '"_republic"' in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "openrouter"])
async def test_chat_factory_tool_history_and_usage(provider: str, tmp_path: Path) -> None:
    config = settings("chat", model=f"{provider}:fixture", republic_protocols={}, api_base=None)
    bodies = [Body(chat(tool=True)), Body(chat())]
    transport = Transport(bodies)
    calls = []

    def inspect(value: int) -> str:
        calls.append(value)
        return "ok"

    with sdk_transport(transport) as clients:
        runner = ModelRunner(config)
        tape = tape_at(tmp_path)
        await collect(runner, tape, tools=[Tool.from_callable(inspect)])
        events = await collect(runner, tape, prompt=None)
        assert calls == [2] and len(transport.requests) == 2
        assert events[-1].data["text"] == "finished"
        assert all(client.is_closed for client in clients) and all(body.closed == 1 for body in bodies)
    request = transport.requests[0]
    assert str(request.url) == (
        "https://api.openai.com/v1/chat/completions"
        if provider == "openai"
        else "https://openrouter.ai/api/v1/chat/completions"
    )
    assert transport.payload()["stream_options"] == {"include_usage": True}
    assert transport.payload(1)["messages"][2] == {"role": "tool", "tool_call_id": "call-original", "content": "ok"}
    info = await tape.info()
    assert info.last_token_usage == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
@pytest.mark.parametrize("mode", ["raises", "denied"])
async def test_tool_error_bit_survives_hooks_and_next_request(protocol: str, mode: str, tmp_path: Path) -> None:
    invoked = []
    observed = []

    def inspect(value: int) -> str:
        invoked.append(value)
        raise ValueError("tool failed")

    class Hooks:
        @hookimpl
        def before_tool_call(self, call: Any, state: dict) -> ToolCallDecision | None:
            return ToolCallDecision.deny("denied") if mode == "denied" else None

        @hookimpl
        def after_tool_call(self, call: Any, result: ToolCallResult, state: dict) -> None:
            observed.append(result.error)
            result.result = "caller-visible failure"

    config = settings(protocol)
    transport = Transport([Body(wire(protocol, tool=True)), Body(wire(protocol))])
    with sdk_transport(transport):
        runner = ModelRunner(config, hooks=make_hooks(Hooks()))
        await collect(runner, tape_at(tmp_path), tools=[Tool.from_callable(inspect)])
        await collect(runner, tape_at(tmp_path), prompt=None)
    assert invoked == ([2] if mode == "raises" else []) and len(observed) == 1 and observed[0] is not None
    payload = transport.payload(1)
    if protocol == "messages":
        result = payload["messages"][-1]["content"][0]
        assert result["is_error"] is True and result["content"] == "caller-visible failure"
    else:
        content = payload["input"][-1]["output"] if protocol == "responses" else payload["messages"][-1]["content"]
        assert json.loads(content) == {"is_error": True, "result": "caller-visible failure"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol,outcome",
    [
        ("responses", "incomplete"),
        ("responses", "failed"),
        ("messages", "max_tokens"),
        ("messages", "pause_turn"),
        ("responses", "bad_json"),
    ],
)
async def test_unfinished_or_malformed_tool_call_never_executes(protocol: str, outcome: str, tmp_path: Path) -> None:
    called = []
    after = []

    class Observe:
        @hookimpl
        def after_llm_call(self, request: Any, result: LlmCallResult, state: dict) -> None:
            after.append(result)

    kwargs = {"status": outcome} if protocol == "responses" else {"stop": outcome}
    if outcome == "bad_json":
        kwargs = {"arguments": '{"value":'}
    transport = Transport([Body(wire(protocol, tool=True, **kwargs))])
    with sdk_transport(transport):
        runner = ModelRunner(settings(protocol), hooks=make_hooks(Observe()))
        events = await collect(
            runner, tape_at(tmp_path), tools=[Tool(name="inspect", handler=lambda **_: called.append(1))]
        )
    assert not called and len(transport.requests) == 1
    assert len(after) == 1 and after[0].error is not None
    assert events[-1].data["ok"] is False and not any(item.kind == "tool_call" for item in events)
    tape = tape_at(tmp_path)
    assert await tape.read_messages() == [{"role": "user", "content": "hello"}]
    entries = list(await tape.store.fetch_all(tape.query()))
    native = [entry for entry in entries if entry.kind == "message" and "_republic" in entry.payload]
    assert len(native) == 1 and native[0].meta["context"] is False
    assert native[0].payload["_republic"]["message"]["parts"]


def partial(protocol: str) -> bytes:
    if protocol == "messages":
        return messages().split(b"event: content_block_stop")[0]
    if protocol == "chat":
        return chat().split(b"\n\n")[0] + b"\n\n"
    return sse([
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": "m", "role": "assistant", "status": "in_progress", "content": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "m",
            "output_index": 0,
            "content_index": 0,
            "delta": "partial",
        },
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
@pytest.mark.parametrize("mode", ["close", "cancel", "missing"])
async def test_stream_exit_and_missing_terminal_release_resources(protocol: str, mode: str, tmp_path: Path) -> None:
    after = []

    class Observe:
        @hookimpl
        def after_llm_call(self, request: Any, result: LlmCallResult, state: dict) -> None:
            after.append(result)

    body = Body(partial(protocol), wait=mode != "missing")
    transport = Transport([body])
    config = settings(protocol, fallback_models=[settings(protocol).model.replace("fixture-model", "fallback")])
    runner = ModelRunner(config, hooks=make_hooks(Observe()))
    with sdk_transport(transport) as clients:
        tape = tape_at(tmp_path)
        await tape.ensure_bootstrap_anchor()
        output = runner.run(tape=tape, model=config.model, tools=[], system_prompt=None, prompt="hello")
        async with aclosing(output):
            assert (await anext(output)).kind == "text"
            if mode == "cancel":
                task = asyncio.create_task(anext(output))
                await asyncio.wait_for(body.waiting.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode == "missing":
                with pytest.raises(republic.IncompleteStreamError):
                    await anext(output)
        assert all(client.is_closed for client in clients)
    assert body.closed == 1 and len(transport.requests) == 1
    assert len(after) == (1 if mode == "missing" else 0)
    assert not await tape_at(tmp_path).read_messages()


@pytest.mark.asyncio
async def test_explicit_bub_fallback_and_hooks_surround_two_single_calls(tmp_path: Path) -> None:
    after = []

    class Hooks:
        @hookimpl
        def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
            return replace(request, messages=[*request.messages, {"role": "user", "content": "hook-added"}])

        @hookimpl
        def after_llm_call(self, request: Any, result: LlmCallResult, state: dict) -> None:
            after.append(result)

    config = settings(fallback_models=["openai:second"])
    transport = Transport([httpx.Response(503, json={"error": {"message": "temporary"}}), Body(responses())])
    with sdk_transport(transport) as clients:
        result = await collect(ModelRunner(config, hooks=make_hooks(Hooks())), tape_at(tmp_path))
        assert len(clients) == 2 and all(client.is_closed for client in clients)
    assert len(transport.requests) == 2 and len(after) == 1 and after[0].error is None
    assert [transport.payload(i)["model"] for i in range(2)] == ["fixture-model", "second"]
    assert transport.payload()["input"][-1]["content"][0]["text"] == "hook-added"
    assert result[-1].data["ok"] is True


@pytest.mark.asyncio
async def test_hook_short_circuit_makes_no_request(tmp_path: Path) -> None:
    class Hooks:
        @hookimpl
        def before_llm_call(self, request: Any, state: dict) -> LlmCallDecision:
            return LlmCallDecision.finish("intercepted")

    transport = Transport([])
    with sdk_transport(transport):
        result = await collect(ModelRunner(settings(), hooks=make_hooks(Hooks())), tape_at(tmp_path))
    assert not transport.requests and result[-1].data["text"] == "intercepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["messages", "any_llm", "edited"])
async def test_native_history_cannot_be_silently_migrated(switch: str, tmp_path: Path) -> None:
    with sdk_transport(Transport([Body(responses())])):
        await collect(ModelRunner(settings()), tape_at(tmp_path))
    config = (
        settings("messages")
        if switch == "messages"
        else settings(model_backend=switch if switch == "any_llm" else "republic")
    )

    class Edit:
        @hookimpl
        def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
            if switch == "edited":
                history = [dict(message) for message in request.messages]
                history[1]["content"] = "changed"
                return replace(request, messages=history)
            return request

    transport = Transport([])
    with sdk_transport(transport), pytest.raises(BubError):
        await collect(ModelRunner(config, hooks=make_hooks(Edit())), tape_at(tmp_path), prompt=None)
    assert not transport.requests


@pytest.mark.asyncio
async def test_tape_failure_after_tool_does_not_retry_or_emit_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []
    tape = tape_at(tmp_path)

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk failure")

    await tape.ensure_bootstrap_anchor()
    monkeypatch.setattr(tape.store, "append", fail)
    transport = Transport([Body(responses(tool=True))])
    with sdk_transport(transport), pytest.raises(OSError, match="disk failure"):
        await collect(ModelRunner(settings()), tape, tools=[Tool(name="inspect", handler=lambda **_: called.append(1))])
    assert called == [1] and len(transport.requests) == 1


@pytest.mark.asyncio
async def test_actual_agent_owns_the_second_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bub.builtin.agent import Agent
    from bub.framework import BubFramework
    from bub.store import FileTapeStore

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    config = settings(max_steps=3)
    monkeypatch.setattr("bub.builtin.agent.load_settings", lambda: config)
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    called = []
    tool = Tool(name="inspect", handler=lambda **_: called.append(1) or "ok")
    agent = Agent(framework, tools=[tool], tape_store=FileTapeStore(tmp_path / "tapes"), skill_dirs=[])
    transport = Transport([Body(responses(tool=True)), Body(responses())])
    with sdk_transport(transport):
        output = await agent.run_stream(session_id="integration", prompt="hello", state={"workspace": str(tmp_path)})
        async with aclosing(output):
            events = [item async for item in output]
    assert called == [1] and len(transport.requests) == 2
    assert sum(item.kind == "final" for item in events) == 2 and events[-1].data["text"] == "finished"
    assert any(item["type"] == "function_call_output" for item in transport.payload(1)["input"] if "type" in item)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"model": "gemini:fixture"},
        {"client_args": {"unhandled": True}},
        {"api_key": None},
        {"republic_protocols": {"openai": "messages"}},
        {"completion_args": {"model": "override"}},
        {"completion_args": {"max_output_tokens": 5}},
    ],
)
async def test_unsupported_configuration_never_falls_back_to_any_llm(extra: dict[str, Any], tmp_path: Path) -> None:
    transport = Transport([])
    with sdk_transport(transport), pytest.raises((BubError, ValueError)):
        await collect(ModelRunner(settings(**extra)), tape_at(tmp_path))
    assert not transport.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
async def test_borrowed_client_remains_open_after_stream_close(
    protocol: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import openai
    from republic.providers.anthropic import AnthropicMessages
    from republic.providers.openai import OpenAIChatCompletions, OpenAIResponses

    bodies = [Body(partial(protocol), wait=True), Body(wire(protocol))]
    transport = Transport(bodies)
    async with httpx.AsyncClient(transport=transport) as http:
        sdk = (
            anthropic.AsyncAnthropic(api_key="fixture-key", http_client=http, max_retries=4)
            if protocol == "messages"
            else openai.AsyncOpenAI(api_key="fixture-key", http_client=http, max_retries=4)
        )
        adapter = {"messages": AnthropicMessages, "responses": OpenAIResponses, "chat": OpenAIChatCompletions}[protocol]
        runner = ModelRunner(settings(protocol))
        monkeypatch.setattr(runner, "create_republic_provider", lambda _: adapter(client=sdk))
        tape = tape_at(tmp_path)
        await tape.ensure_bootstrap_anchor()
        output = runner.run(tape=tape, model=runner.settings.model, tools=[], system_prompt=None, prompt="hello")
        async with aclosing(output):
            await anext(output)
        assert not http.is_closed and bodies[0].closed == 1 and sdk.max_retries == 4
        await collect(runner, tape)
        assert not http.is_closed and bodies[1].closed == 1 and len(transport.requests) == 2


@pytest.mark.asyncio
async def test_republic_selection_cannot_call_legacy_completion_method() -> None:
    with pytest.raises(BubError, match="any-llm completions are disabled"):
        await ModelRunner(settings()).completion_response(model="openai:fixture", messages=[], tools=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_http_failures_fire_after_hook_once_without_retry(status: int, tmp_path: Path) -> None:
    observed = []

    class Observe:
        @hookimpl
        def after_llm_call(self, request: Any, result: LlmCallResult, state: dict) -> None:
            observed.append(result)

    transport = Transport([httpx.Response(status, json={"error": {"message": "fixture"}})])
    with sdk_transport(transport) as clients, pytest.raises(republic.ProviderError):
        await collect(ModelRunner(settings(), hooks=make_hooks(Observe())), tape_at(tmp_path))
    assert len(observed) == 1 and observed[0].error is not None and len(transport.requests) == 1
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://fixture.test/image"}}]},
        {"role": "assistant", "content": "text", "unknown_metadata": "must not drop"},
        {"role": "assistant", "content": None, "tool_calls": {}},
        {"role": "assistant", "content": "text", "tool_call_id": "extra"},
        {"role": "tool", "content": "result", "tool_call_id": "call-original"},
        {"role": "tool", "content": "result", "tool_call_id": "call-original", "name": "inspect", "tool_calls": []},
    ],
)
async def test_unrepresentable_legacy_history_fails_before_http(message: dict, tmp_path: Path) -> None:
    class History:
        @hookimpl
        def before_llm_call(self, request: LlmCallRequest, state: dict) -> LlmCallRequest:
            return replace(request, messages=[message])

    transport = Transport([])
    with sdk_transport(transport), pytest.raises(BubError):
        await collect(ModelRunner(settings(), hooks=make_hooks(History())), tape_at(tmp_path))
    assert not transport.requests


def test_backend_settings_are_explicit_and_validate_protocol_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from bub.builtin.settings import AgentSettings

    monkeypatch.setenv("BUB_MODEL_BACKEND", "republic")
    monkeypatch.setenv("BUB_REPUBLIC_PROTOCOLS", '{"openai":"responses"}')
    config = AgentSettings(_env_file=None)
    assert config.model_backend == "republic" and config.republic_protocols == {"openai": "responses"}
    monkeypatch.setenv("BUB_REPUBLIC_PROTOCOLS", '{"openai":"auto"}')
    with pytest.raises(ValidationError):
        AgentSettings(_env_file=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["responses", "messages", "chat"])
async def test_tape_records_actual_response_identity_and_inclusive_usage(protocol: str, tmp_path: Path) -> None:
    transport = Transport([Body(wire(protocol))])
    tape = tape_at(tmp_path)
    with sdk_transport(transport):
        await collect(ModelRunner(settings(protocol)), tape)
    entries = list(await tape.store.fetch_all(tape.query()))
    run = next(entry.payload["data"] for entry in entries if entry.kind == "event" and entry.payload["name"] == "run")
    assert run["response_model"] == "resolved-model" and run["finish_reason"] == "stop"
    assert (
        run["response_id"]
        == {"responses": "response-original", "messages": "message-original", "chat": "chat-original"}[protocol]
    )
    assert run["usage"]["input_tokens"] == (15 if protocol == "messages" else 10)
    assert run["usage"]["total_tokens"] == (20 if protocol == "messages" else 15)
    assert run["usage"]["raw"]
