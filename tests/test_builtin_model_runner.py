from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import httpx2
import pytest
import republic
from republic.errors import APIStatusError, StreamIncompleteError

from bub.builtin.context import default_tape_context
from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import AgentSettings
from bub.errors import BubError, ErrorKind
from bub.prompt import to_content
from bub.store import AsyncTapeStoreAdapter, FileTapeStore, InMemoryTapeStore
from bub.tape import Tape, TapeContext, TapeEntry
from bub.tools import Tool
from tests.model_fakes import ProviderService, chat_events, sse, tool_events


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "http_error", "incomplete"])
async def test_model_call_closes_its_owned_client(
    tmp_path: Path, provider_service: ProviderService, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    clients = []
    constructor = httpx2.AsyncClient

    def create_client(**kwargs):
        client = constructor(**kwargs, transport=httpx2.MockTransport(provider_service._respond))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx2, "AsyncClient", create_client)
    error = None
    if outcome == "http_error":
        provider_service.reply(httpx2.Response(401, json={"error": "unauthorized"}))
        error = APIStatusError
    else:
        provider_service.reply(sse(chat_events() if outcome == "success" else chat_events()[:-1]))
        if outcome == "incomplete":
            error = StreamIncompleteError
    runner = ModelRunner(AgentSettings(client_args={"api_format": "chat", "max_retries": 0}))
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("owned")
    with pytest.raises(error) if error is not None else nullcontext():
        events = [
            event
            async for event in runner.run(tape=tape, model="openai:test", tools=[], system_prompt=None, prompt="hello")
        ]
        assert events[-1].data == {"ok": True, "text": "done"}
    assert len(clients) == 1
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_unknown_tool_returns_an_error_result(tmp_path: Path, provider_service: ProviderService) -> None:
    provider_service.reply(
        sse(
            tool_events([
                {"index": 0, "id": "call-1", "function": {"name": "missing_tool", "arguments": "{}"}},
            ])
        )
    )
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client, "api_format": "chat"}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("unknown")
        events = [
            event
            async for event in runner.run(
                tape=tape, model="openai:test", tools=[], system_prompt=None, prompt=to_content("Run the tool.")
            )
        ]
    result = next(event.data["tool_results"][0] for event in events if event.kind == "tool_result")
    assert result["kind"] == "tool"
    assert "missing_tool" in result["message"]
    assert events[-1].kind == "final"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "Check byte equality.\nOnly exact equality counts."])
@pytest.mark.parametrize("continuation_prompt", ["Continue.", "", None])
async def test_tool_turn_replays_after_tape_reload(
    tmp_path: Path, provider_service: ProviderService, content: str, continuation_prompt: str | None
) -> None:
    provider_service.reply(
        sse(
            tool_events(
                [
                    {"index": index, "id": f"call-{name}", "function": {"name": name, "arguments": "{}"}}
                    for index, name in enumerate(("inspect", "compare"))
                ],
                text=content,
            )
        )
    )
    provider_service.reply(sse(chat_events("Compared.")))
    tools = [Tool(name="inspect", handler=lambda: "files found"), Tool(name="compare", handler=lambda: "bytes differ")]
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client, "api_format": "chat"}))
        root = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped("turn")
        async with root.fork_tape() as tape:
            await tape.ensure_bootstrap_anchor()
            events = [
                event
                async for event in runner.run(
                    tape=tape,
                    model="openai:test",
                    tools=tools,
                    system_prompt=None,
                    prompt=to_content("Compare the outputs."),
                )
            ]
        reopened = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped("turn")
        follow_up = [
            event
            async for event in runner.run(
                tape=reopened,
                model="openai:test",
                tools=tools,
                system_prompt=None,
                prompt=None if continuation_prompt is None else to_content(continuation_prompt),
            )
        ]
    assert next(event.data["tool_results"] for event in events if event.kind == "tool_result") == [
        "files found",
        "bytes differ",
    ]
    messages = provider_service.body()["messages"]
    users = [message["content"] for message in messages if message["role"] == "user"]
    assert users == ["Compare the outputs.", *([continuation_prompt] if continuation_prompt is not None else [])]
    assistant = next(message for message in messages if message["role"] == "assistant")
    assert (assistant.get("content") or "") == content
    assert {call["id"]: call["function"]["name"] for call in assistant["tool_calls"]} == {
        "call-inspect": "inspect",
        "call-compare": "compare",
    }
    assert {message["tool_call_id"]: message["content"] for message in messages if message["role"] == "tool"} == {
        "call-inspect": "files found",
        "call-compare": "bytes differ",
    }
    assert follow_up[-1].data == {"ok": True, "text": "Compared."}


@pytest.mark.asyncio
async def test_continuation_sends_steering_without_a_prompt(tmp_path: Path, provider_service: ProviderService) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client, "api_format": "chat"}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), default_tape_context()).scoped("steering")
        await tape.ensure_bootstrap_anchor()
        events = [
            event
            async for event in runner.run(
                tape=tape,
                model="openai:test",
                tools=[],
                system_prompt=None,
                prompt=None,
                steering_messages=[["new user direction"]],
            )
        ]
    assert provider_service.body()["messages"] == [{"role": "user", "content": "new user direction"}]
    assert events[-1].data == {"ok": True, "text": "done"}
    assert republic.user("new user direction") in await tape.read_messages()


@pytest.mark.asyncio
async def test_tool_result_without_its_call_in_context_is_still_sent(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client, "api_format": "chat"}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("orphan")
        await tape.store.append(
            tape.name,
            TapeEntry.message({"role": "tool", "tool_call_id": "call-1", "name": "inspect", "content": "Ready"}),
        )
        events = [
            event
            async for event in runner.run(
                tape=tape, model="openai:test", tools=[], system_prompt="Be brief.", prompt=to_content("Continue.")
            )
        ]
    messages = provider_service.body()["messages"]
    assert messages[0] == {"role": "system", "content": "Be brief."}
    assert messages[1]["tool_calls"][0]["id"] == "call-1"
    assert messages[1]["tool_calls"][0]["function"]["name"] == "inspect"
    assert messages[2] == {"role": "tool", "tool_call_id": "call-1", "content": "Ready"}
    assert events[-1].data == {"ok": True, "text": "done"}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["[]", "null", "1", "not json"])
async def test_invalid_tool_arguments_do_not_execute_the_handler(
    tmp_path: Path, provider_service: ProviderService, arguments: str
) -> None:
    provider_service.reply(
        sse(tool_events([{"index": 0, "id": "call-1", "function": {"name": "inspect", "arguments": arguments}}]))
    )
    invoked = []
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client, "api_format": "chat"}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("invalid")
        with pytest.raises(BubError) as exc:
            _ = [
                event
                async for event in runner.run(
                    tape=tape,
                    model="openai:test",
                    tools=[Tool(name="inspect", handler=lambda: invoked.append(True))],
                    system_prompt=None,
                    prompt=to_content("Inspect."),
                )
            ]
    assert exc.value.kind == ErrorKind.INVALID_INPUT
    assert not invoked
    assert not await tape.store.fetch_all(tape.query().kinds("tool_result"))


@pytest.mark.asyncio
async def test_streaming_reports_usage_and_records_it_in_tape(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(max_tokens=100, client_args={"http_client": client, "api_format": "chat"}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("usage")
        events = [
            event
            async for event in runner.run(
                tape=tape, model="openai:test", tools=[], system_prompt=None, prompt=to_content("hello")
            )
        ]
    assert provider_service.body()["stream_options"] == {"include_usage": True}
    usage = next(event.data for event in events if event.kind == "usage")
    assert usage["usage"]["input_tokens"] == 3
    assert usage["usage"]["output_tokens"] == 2
    assert usage["usage"]["total_tokens"] == 5
    assert usage["elapsed_seconds"] >= 0
    assert events[-1].data == {"ok": True, "text": "done"}
    assert (await tape.info()).last_token_usage == 5


@pytest.mark.asyncio
async def test_anthropic_request_enables_caching_and_generation_options(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(
        sse([
            {"type": "message_start", "message": {"id": "test", "usage": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
            {"type": "message_stop"},
        ])
    )
    async with provider_service.client() as client:
        runner = ModelRunner(
            AgentSettings(max_tokens=100, client_args={"http_client": client}, completion_args={"temperature": 0.2})
        )
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("cache")
        events = [
            event
            async for event in runner.run(
                tape=tape, model="anthropic:test", tools=[], system_prompt=None, prompt=to_content("hello")
            )
        ]
    assert provider_service.body()["cache_control"] == {"type": "ephemeral"}
    assert provider_service.body()["temperature"] == 0.2
    assert provider_service.body()["max_tokens"] == 100
    assert events[-1].data == {"ok": True, "text": "done"}


@pytest.mark.asyncio
async def test_session_reasoning_and_runtime_options_override_completion_defaults(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        runner = ModelRunner(
            AgentSettings(
                client_args={
                    "http_client": client,
                    "api_format": "chat",
                    "extra_body": {"metadata": {"client": True}},
                },
                completion_args={
                    "reasoning_effort": "low",
                    "tools": [republic.Tool("ignored")],
                    "max_tokens": 1,
                    "extra_body": {"metadata": {"request": True}},
                },
            )
        )
        tape = Tape(
            tmp_path,
            AsyncTapeStoreAdapter(InMemoryTapeStore()),
            TapeContext(anchor=None, state={"reasoning_effort": "high"}),
        ).scoped("reasoning")
        _ = [
            event
            async for event in runner.run(
                tape=tape, model="openai:test", tools=[], system_prompt=None, prompt=to_content("hello")
            )
        ]
    request = provider_service.body()
    assert request["reasoning_effort"] == "high"
    assert request["model"] == "test"
    assert request["messages"] == [{"role": "user", "content": "hello"}]
    assert request["max_completion_tokens"] == 16384
    assert request["stream"] is True
    assert "tools" not in request
    assert request["metadata"] == {"client": True, "request": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("request_reasoning", [None, "medium"])
async def test_wire_reasoning_follows_republic_extra_body_precedence(
    tmp_path: Path, provider_service: ProviderService, request_reasoning: str | None
) -> None:
    provider_service.reply(sse(chat_events()))
    async with provider_service.client() as client:
        runner = ModelRunner(
            AgentSettings(
                client_args={"http_client": client, "api_format": "chat", "extra_body": {"reasoning_effort": "low"}},
                completion_args={"extra_body": {"reasoning_effort": request_reasoning} if request_reasoning else {}},
            )
        )
        tape = Tape(
            tmp_path,
            AsyncTapeStoreAdapter(InMemoryTapeStore()),
            TapeContext(anchor=None, state={"reasoning_effort": "high"}),
        ).scoped("wire-reasoning")
        _ = [
            event
            async for event in runner.run(
                tape=tape, model="openai:test", tools=[], system_prompt=None, prompt=to_content("hello")
            )
        ]
    assert provider_service.body()["reasoning_effort"] == (request_reasoning or "low")


@pytest.mark.asyncio
async def test_truncated_stream_never_executes_tools(tmp_path: Path, provider_service: ProviderService) -> None:
    provider_service.reply(
        sse([
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call-1", "function": {"name": "echo", "arguments": "{}"}}
                            ]
                        }
                    }
                ]
            }
        ])
    )
    invoked = []
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client, "api_format": "chat"}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("truncated")
        with pytest.raises(republic.errors.StreamIncompleteError):
            _ = [
                event
                async for event in runner.run(
                    tape=tape,
                    model="openai:test",
                    tools=[Tool(name="echo", handler=lambda: invoked.append(True))],
                    system_prompt=None,
                    prompt=to_content("hello"),
                )
            ]
    assert not invoked
    assert not await tape.store.fetch_all(tape.query().kinds("tool_call", "tool_result"))


@pytest.mark.asyncio
@pytest.mark.parametrize("next_model", ["google:model-one", "google:model-two", "openai:model-one"])
async def test_provider_state_is_replayed_only_to_its_api_format_after_reload(
    tmp_path: Path, provider_service: ProviderService, next_model: str
) -> None:
    opaque = {"executableCode": {"language": "PYTHON", "code": "1 + 1"}}
    provider_service.reply(
        sse([
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                opaque,
                                {
                                    "functionCall": {"id": "call-1", "name": "lookup", "args": {}},
                                    "thoughtSignature": "signature",
                                },
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
        ])
    )
    provider_service.reply(
        sse(
            chat_events()
            if next_model.startswith("openai:")
            else [{"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}]
        )
    )
    async with provider_service.client() as client:
        runner = ModelRunner(AgentSettings(client_args={"http_client": client}))
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped("metadata")
        await tape.ensure_bootstrap_anchor()
        tools = [Tool(name="lookup", handler=lambda: "lookup result")]
        _ = [
            event
            async for event in runner.run(
                tape=tape, model="google:model-one", tools=tools, system_prompt=None, prompt=to_content("lookup")
            )
        ]
        reopened = Tape(tmp_path, AsyncTapeStoreAdapter(FileTapeStore(tmp_path)), default_tape_context()).scoped(
            "metadata"
        )
        if next_model.startswith("openai:"):
            runner.settings.client_args["api_format"] = "chat"
        events = [
            event
            async for event in runner.run(
                tape=reopened, model=next_model, tools=tools, system_prompt=None, prompt=to_content("Continue.")
            )
        ]
    request = json.dumps(provider_service.body())
    assert "lookup result" in request
    assert ("signature" in request) is next_model.startswith("google:")
    assert ("executableCode" in request) is next_model.startswith("google:")
    assert events[-1].data == {"ok": True, "text": "done"}


@pytest.mark.asyncio
async def test_named_tool_choice_is_sent_to_anthropic(tmp_path: Path, provider_service: ProviderService) -> None:
    provider_service.reply(
        sse([
            {"type": "message_start", "message": {"id": "test", "usage": {}}},
            {"type": "message_stop"},
        ])
    )
    async with provider_service.client() as client:
        runner = ModelRunner(
            AgentSettings(client_args={"http_client": client}, completion_args={"tool_choice": republic.Tool("echo")})
        )
        tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("choice")
        _ = [
            event
            async for event in runner.run(
                tape=tape,
                model="anthropic:test",
                tools=[Tool(name="echo", handler=lambda: "done")],
                system_prompt=None,
                prompt=to_content("hello"),
            )
        ]
    assert provider_service.body()["tool_choice"] == {"type": "tool", "name": "echo"}
