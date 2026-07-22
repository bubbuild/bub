from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from any_llm.constants import LLMProvider
from any_llm.types.completion import CompletionParams
from any_llm.types.responses import ResponsesParams

from bub.builtin.auth import (
    OpenAICodexOAuthTokens,
    extract_openai_codex_account_id,
    load_openai_codex_oauth_tokens,
    openai_codex_oauth_resolver,
    save_openai_codex_oauth_tokens,
)
from bub.builtin.codex_provider import (
    DEFAULT_CODEX_INCLUDE,
    DEFAULT_CODEX_INSTRUCTIONS,
    DEFAULT_CODEX_TEXT_CONFIG,
    OpenaiCodexProvider,
    build_openai_codex_default_headers,
    resolve_openai_codex_api_base,
    should_use_openai_codex_provider,
)
from bub.builtin.model_runner import ModelOutputAccumulator, ModelRunner
from bub.builtin.settings import ModelCandidate

TEST_REFRESH_TOKEN = "refresh"  # noqa: S105
TEST_REFRESH_TOKEN_OLD = "refresh_old"  # noqa: S105
TEST_REFRESH_TOKEN_NEW = "refresh_new"  # noqa: S105


def _jwt_with_account(account_id: str) -> str:
    header = _b64({"alg": "none"})
    payload = _b64({"https://api.openai.com/auth": {"chatgpt_account_id": account_id}})
    return f"{header}.{payload}.sig"


def _b64(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_openai_codex_oauth_tokens_round_trip(tmp_path: Path) -> None:
    tokens = OpenAICodexOAuthTokens(
        access_token=_jwt_with_account("acct_123"),
        refresh_token=TEST_REFRESH_TOKEN,
        expires_at=1_900_000_000,
        account_id="acct_123",
    )

    auth_path = save_openai_codex_oauth_tokens(tokens, tmp_path)
    loaded = load_openai_codex_oauth_tokens(tmp_path)

    assert auth_path == tmp_path / "auth.json"
    assert loaded == tokens
    assert auth_path.stat().st_mode & 0o777 == 0o600


def test_openai_codex_oauth_resolver_refreshes_expired_token(tmp_path: Path) -> None:
    save_openai_codex_oauth_tokens(
        OpenAICodexOAuthTokens(
            access_token=_jwt_with_account("acct_old"),
            refresh_token=TEST_REFRESH_TOKEN_OLD,
            expires_at=int(time.time()) - 1,
            account_id="acct_old",
        ),
        tmp_path,
    )
    refreshed = OpenAICodexOAuthTokens(
        access_token=_jwt_with_account("acct_new"),
        refresh_token=TEST_REFRESH_TOKEN_NEW,
        expires_at=int(time.time()) + 3600,
        account_id="acct_new",
    )

    resolver = openai_codex_oauth_resolver(tmp_path, refresher=lambda refresh_token: refreshed)

    assert resolver("openai") == refreshed.access_token
    assert load_openai_codex_oauth_tokens(tmp_path) == refreshed


def test_extract_openai_codex_account_id() -> None:
    assert extract_openai_codex_account_id(_jwt_with_account("acct_123")) == "acct_123"
    assert extract_openai_codex_account_id("not-a-jwt") is None


def test_codex_provider_selection_requires_oauth_file_or_oauth_token(monkeypatch) -> None:
    monkeypatch.setattr(
        "bub.builtin.codex_provider.load_openai_codex_oauth_tokens",
        lambda: OpenAICodexOAuthTokens(
            access_token=_jwt_with_account("acct_123"),
            refresh_token=TEST_REFRESH_TOKEN,
            expires_at=1_900_000_000,
        ),
    )

    assert should_use_openai_codex_provider("openai", "gpt-5.5", api_key=None, api_base=None) is True
    assert (
        should_use_openai_codex_provider("openai", "gpt-4o", api_key=_jwt_with_account("acct_123"), api_base=None)
        is True
    )
    assert should_use_openai_codex_provider("openai", "gpt-5-codex", api_key="sk-test", api_base=None) is False
    assert should_use_openai_codex_provider("openai", "gpt-5-codex", api_key=None, api_base="https://api.test") is False


def test_codex_provider_selection_uses_normal_openai_without_oauth(monkeypatch) -> None:
    monkeypatch.setattr("bub.builtin.codex_provider.load_openai_codex_oauth_tokens", lambda: None)

    assert should_use_openai_codex_provider("openai", "gpt-5.5", api_key=None, api_base=None) is False


def test_model_runner_creates_codex_provider_for_codex_model(monkeypatch) -> None:
    fake_provider = MagicMock()
    provider_class = MagicMock(return_value=fake_provider)
    monkeypatch.setattr("bub.builtin.model_runner.OpenaiCodexProvider", provider_class)
    monkeypatch.setattr(
        "bub.builtin.codex_provider.load_openai_codex_oauth_tokens",
        lambda: OpenAICodexOAuthTokens(
            access_token=_jwt_with_account("acct_123"),
            refresh_token=TEST_REFRESH_TOKEN,
            expires_at=1_900_000_000,
        ),
    )
    candidate = ModelCandidate(provider=LLMProvider.OPENAI, model_id="gpt-5.5", name="openai:gpt-5.5")

    client = ModelRunner.create_llm_client(candidate, {"api_key": None, "api_base": None})

    assert client is fake_provider
    provider_class.assert_called_once_with(api_key=None, api_base=None)


def test_codex_provider_adds_response_defaults() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    params = ResponsesParams(model="gpt-5-codex", input="hello", stream=True, text={"format": {"type": "text"}})

    prepared = provider._with_codex_response_defaults(params)

    assert prepared.store is False
    assert prepared.instructions == DEFAULT_CODEX_INSTRUCTIONS
    assert prepared.include == DEFAULT_CODEX_INCLUDE
    assert prepared.text == {**DEFAULT_CODEX_TEXT_CONFIG, "format": {"type": "text"}}


def test_codex_provider_preserves_explicit_response_options() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    params = ResponsesParams(
        model="gpt-5-codex",
        input="hello",
        instructions="custom",
        include=[],
        store=True,
        text={"verbosity": "low"},
    )

    prepared = provider._with_codex_response_defaults(params)

    assert prepared.store is True
    assert prepared.instructions == "custom"
    assert prepared.include == []
    assert prepared.text == {**DEFAULT_CODEX_TEXT_CONFIG, "verbosity": "low"}


def test_codex_completion_params_use_official_responses_payload_fields() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    params = CompletionParams(
        model_id="gpt-5.5",
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=100,
        temperature=0.2,
        top_p=0.9,
        presence_penalty=0.1,
        frequency_penalty=0.1,
        user="user_123",
        stream=True,
        stream_options={"include_usage": True},
    )

    responses_params = provider._completion_params_to_responses_params(params)
    payload = responses_params.model_dump(exclude_none=True, exclude={"response_format"})

    assert payload == {
        "model": "gpt-5.5",
        "input": [{"role": "user", "content": "hello"}],
        "stream": True,
    }


def test_codex_completion_params_convert_chat_tool_messages_to_responses_items() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    params = CompletionParams(
        model_id="gpt-5.5",
        messages=[
            {"role": "user", "content": "run bash"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"cmd":"pwd"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "name": "bash", "content": "workspace"},
        ],
        stream=True,
    )

    responses_params = provider._completion_params_to_responses_params(params)
    payload = responses_params.model_dump(exclude_none=True, exclude={"response_format"})

    assert payload == {
        "model": "gpt-5.5",
        "input": [
            {"role": "user", "content": "run bash"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "bash",
                "arguments": '{"cmd":"pwd"}',
                "status": "completed",
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "workspace"},
        ],
        "stream": True,
    }


def test_codex_provider_resolves_codex_api_base_and_headers() -> None:
    token = _jwt_with_account("acct_123")

    assert resolve_openai_codex_api_base(None) == "https://chatgpt.com/backend-api/codex"
    assert resolve_openai_codex_api_base("https://example.test/responses") == "https://example.test/codex"
    assert build_openai_codex_default_headers(token) == {
        "chatgpt-account-id": "acct_123",
        "OpenAI-Beta": "responses=experimental",
        "originator": "bub",
    }


async def _codex_response_events():
    yield SimpleNamespace(type="response.output_text.delta", delta="hel")
    yield SimpleNamespace(type="response.output_text.delta", delta="lo")
    yield SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            id="resp_123",
            created_at=1,
            model="gpt-5-codex",
            usage=SimpleNamespace(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                input_tokens_details={"cached_tokens": 2},
            ),
        ),
    )


@pytest.mark.asyncio
async def test_codex_completion_stream_maps_response_events_to_completion_chunks() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    provider._aresponses = AsyncMock(return_value=_codex_response_events())  # type: ignore[method-assign]
    params = CompletionParams(
        model_id="gpt-5-codex",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    completion = await provider._acompletion(params)
    chunks = [chunk async for chunk in completion]

    assert [chunk.choices[0].delta.content for chunk in chunks[:2]] == ["hel", "lo"]
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 3
    assert chunks[-1].usage.completion_tokens == 2
    assert chunks[-1].usage.prompt_tokens_details is not None
    assert chunks[-1].usage.prompt_tokens_details.cached_tokens == 2


async def _codex_tool_response_events():
    yield SimpleNamespace(
        type="response.output_item.added",
        output_index=0,
        item=SimpleNamespace(type="function_call", id="fc_1", call_id="call_1", name="bash", arguments=""),
    )
    yield SimpleNamespace(type="response.function_call_arguments.delta", output_index=0, delta='{"cmd":')
    yield SimpleNamespace(type="response.function_call_arguments.delta", output_index=0, delta='"pwd"}')
    yield SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(id="resp_123", created_at=1, model="gpt-5-codex", usage=None),
    )


@pytest.mark.asyncio
async def test_codex_completion_stream_maps_response_tool_calls_to_completion_chunks() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    provider._aresponses = AsyncMock(return_value=_codex_tool_response_events())  # type: ignore[method-assign]
    params = CompletionParams(
        model_id="gpt-5-codex",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    completion = await provider._acompletion(params)
    chunks = [chunk async for chunk in completion]

    first_tool_delta = chunks[0].choices[0].delta.tool_calls[0]
    assert first_tool_delta.id == "call_1"
    assert first_tool_delta.function.name == "bash"
    assert "".join(chunk.choices[0].delta.tool_calls[0].function.arguments or "" for chunk in chunks[1:3]) == (
        '{"cmd":"pwd"}'
    )
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


async def _codex_custom_tool_response_events():
    yield SimpleNamespace(
        type="response.output_item.added",
        output_index=1,
        item=SimpleNamespace(type="custom_tool_call", id="ctc_1", call_id="call_1", name="bash", input=""),
    )
    yield SimpleNamespace(
        type="response.custom_tool_call_input.delta", item_id="ctc_1", call_id="call_1", delta='{"cmd":'
    )
    yield SimpleNamespace(
        type="response.custom_tool_call_input.delta", item_id="ctc_1", call_id="call_1", delta='"pwd"}'
    )
    yield SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(id="resp_123", created_at=1, model="gpt-5-codex", usage=None),
    )


@pytest.mark.asyncio
async def test_codex_completion_stream_maps_custom_tool_call_input_deltas_to_completion_chunks() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    provider._aresponses = AsyncMock(return_value=_codex_custom_tool_response_events())  # type: ignore[method-assign]
    params = CompletionParams(
        model_id="gpt-5-codex",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    completion = await provider._acompletion(params)
    chunks = [chunk async for chunk in completion]

    first_tool_delta = chunks[0].choices[0].delta.tool_calls[0]
    assert first_tool_delta.index == 1
    assert first_tool_delta.id == "call_1"
    assert first_tool_delta.function.name == "bash"
    assert "".join(chunk.choices[0].delta.tool_calls[0].function.arguments or "" for chunk in chunks[1:3]) == (
        '{"cmd":"pwd"}'
    )
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


async def _codex_tool_done_name_null_response_events():
    yield SimpleNamespace(
        type="response.function_call_arguments.done",
        item_id="fc_1",
        output_index=0,
        name=None,
        arguments='{"message":"hello"}',
    )
    yield SimpleNamespace(
        type="response.output_item.done",
        output_index=0,
        item=SimpleNamespace(
            type="function_call",
            id="fc_1",
            call_id="call_1",
            name="echo",
            arguments='{"message":"hello"}',
            status="completed",
        ),
    )
    yield SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(id="resp_123", created_at=1, model="gpt-5-codex", usage=None),
    )


@pytest.mark.asyncio
async def test_codex_completion_stream_keeps_tool_name_when_arguments_done_name_is_null() -> None:
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))
    provider._aresponses = AsyncMock(return_value=_codex_tool_done_name_null_response_events())  # type: ignore[method-assign]
    params = CompletionParams(
        model_id="gpt-5-codex",
        messages=[{"role": "user", "content": "call echo"}],
        stream=True,
    )

    completion = await provider._acompletion(params)
    output = ModelOutputAccumulator()
    chunks = [chunk async for chunk in completion]
    for chunk in chunks:
        tool_calls = chunk.choices[0].delta.tool_calls
        if tool_calls:
            output.merge_delta_tool_calls(tool_calls)

    tool_call = output.tool_calls[0]
    assert tool_call.id == "call_1"
    assert tool_call.function.name == "echo"
    assert tool_call.function.arguments == '{"message":"hello"}'
    assert chunks[-1].choices[0].finish_reason == "tool_calls"
