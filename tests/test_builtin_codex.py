from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from any_llm.constants import LLMProvider
from any_llm.types.completion import CompletionParams
from any_llm.types.responses import ResponsesParams
from openai.types.responses.response_input_param import ResponseInputParam
from openai.types.responses.response_output_text import ResponseOutputText
from pydantic import TypeAdapter, ValidationError

from bub.builtin.auth import (
    CodexOAuthRefreshError,
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
from bub.channels.message import ChannelMessage, MediaItem
from bub.framework import BubFramework

TEST_REFRESH_TOKEN = "refresh"  # noqa: S105
TEST_REFRESH_TOKEN_OLD = "refresh_old"  # noqa: S105
TEST_REFRESH_TOKEN_NEW = "refresh_new"  # noqa: S105


def _jwt_with_account(account_id: str, *, exp: int | None = None) -> str:
    header = _b64({"alg": "none"})
    claims: dict[str, Any] = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    if exp is not None:
        claims["exp"] = exp
    payload = _b64(claims)
    return f"{header}.{payload}.sig"


def _b64(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _write_codex_auth_without_expiry(home: Path, access_token: str, last_refresh: int) -> None:
    (home / "auth.json").write_text(
        json.dumps({
            "last_refresh": datetime.fromtimestamp(last_refresh, tz=UTC).isoformat(),
            "tokens": {"access_token": access_token, "refresh_token": TEST_REFRESH_TOKEN},
        })
    )


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


def test_codex_auth_without_expires_at_uses_access_token_jwt_exp(tmp_path: Path) -> None:
    now = int(time.time())
    token = _jwt_with_account("acct_123", exp=now + 10 * 86400)
    _write_codex_auth_without_expiry(tmp_path, token, last_refresh=now - 7200)
    refresh_calls: list[str] = []

    def fail_refresh(refresh_token: str) -> OpenAICodexOAuthTokens:
        refresh_calls.append(refresh_token)
        raise RuntimeError("refresh should not run")

    loaded = load_openai_codex_oauth_tokens(tmp_path)
    assert loaded is not None
    assert loaded.expires_at == now + 10 * 86400
    assert openai_codex_oauth_resolver(tmp_path, refresher=fail_refresh)("openai") == token
    assert refresh_calls == []


@pytest.mark.parametrize(
    "expiry_offset, refresh_duration, expected_token", [(60, 0, True), (-60, 0, False), (1, 2, False)]
)
def test_codex_provider_uses_valid_token_or_reports_refresh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expiry_offset: int, refresh_duration: int, expected_token: bool
) -> None:
    now = int(time.time())
    monkeypatch.setattr("bub.builtin.auth.time.time", lambda: now)
    token = _jwt_with_account("acct_123", exp=now + expiry_offset)
    _write_codex_auth_without_expiry(tmp_path, token, last_refresh=now - 7200)

    def fail_refresh(refresh_token: str, **kwargs: Any) -> OpenAICodexOAuthTokens:
        monkeypatch.setattr("bub.builtin.auth.time.time", lambda: now + refresh_duration)
        raise RuntimeError("refresh unavailable")

    monkeypatch.setattr("bub.builtin.auth.refresh_openai_codex_oauth_tokens", fail_refresh)
    if expected_token:
        OpenaiCodexProvider(codex_home=str(tmp_path))
    else:
        with pytest.raises(CodexOAuthRefreshError, match="Codex OAuth"):
            OpenaiCodexProvider(codex_home=str(tmp_path))


def test_codex_auth_explicit_expiry_precedes_jwt_exp_and_missing_claim_stays_unknown(tmp_path: Path) -> None:
    now = int(time.time())
    token = _jwt_with_account("acct_123", exp=now + 10 * 86400)
    _write_codex_auth_without_expiry(tmp_path, token, last_refresh=now - 7200)
    auth_path = tmp_path / "auth.json"
    payload = json.loads(auth_path.read_text())
    payload["tokens"]["expires_at"] = now + 3600
    auth_path.write_text(json.dumps(payload))
    loaded = load_openai_codex_oauth_tokens(tmp_path)
    assert loaded is not None
    assert loaded.expires_at == now + 3600

    for fallback_token in (_jwt_with_account("acct_123"), "not-a-jwt"):
        _write_codex_auth_without_expiry(tmp_path, fallback_token, last_refresh=now - 7200)
        loaded = load_openai_codex_oauth_tokens(tmp_path)
        assert loaded is not None
        assert loaded.expires_at is None
        refresher = MagicMock(side_effect=RuntimeError("refresh should not run"))
        assert openai_codex_oauth_resolver(tmp_path, refresher=refresher)("openai") == fallback_token
        refresher.assert_not_called()


def test_codex_auth_uses_valid_token_when_expiry_metadata_is_invalid(tmp_path: Path) -> None:
    now = int(time.time())
    token = _jwt_with_account("acct_123", exp=now + 86400)
    _write_codex_auth_without_expiry(tmp_path, token, last_refresh=now - 7200)
    auth_path = tmp_path / "auth.json"
    payload = json.loads(auth_path.read_text())
    payload["tokens"]["expires_at"] = "invalid"
    auth_path.write_text(json.dumps(payload))

    refresher = MagicMock(side_effect=RuntimeError("refresh should not run"))
    assert openai_codex_oauth_resolver(tmp_path, refresher=refresher)("openai") == token
    refresher.assert_not_called()


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
                        "function": {"name": "bash", "arguments": '{"command":"pwd"}'},
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
                "arguments": '{"command":"pwd"}',
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


def _validate_codex_input(items: list[dict[str, Any]]) -> None:
    for item in items:
        if item.get("role") == "assistant":
            # Assistant history requires output_text, as the real endpoint does.
            for part in item["content"]:
                ResponseOutputText.model_validate({"annotations": [], **part})
        else:
            TypeAdapter(ResponseInputParam).validate_python([item])


@pytest.mark.asyncio
@pytest.mark.parametrize("content_kind", ["string", "text", "image", "file", "assistant-history"])
async def test_codex_completion_accepts_chat_content_without_bad_request(content_kind: str, tmp_path: Path) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    message = ChannelMessage(session_id="content-test", channel="cli", content="Reply exactly BUB_READY.")
    if content_kind == "image":
        message.media = [MediaItem(type="image", mime_type="image/png", url="https://example.test/image.png")]
    content = await framework.build_prompt(message, message.session_id, {})
    if content_kind == "text":
        content = [{"type": "text", "text": message.content}]
    elif content_kind == "file":
        content = [{"type": "text", "text": message.content}, {"type": "file", "file": {"file_id": "file-test"}}]
    messages = [{"role": "user", "content": content}]
    if content_kind == "assistant-history":
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Checking the previous request."}],
                "tool_calls": [
                    {"id": "call_test", "type": "function", "function": {"name": "check", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_test", "content": "Ready"},
            *messages,
        ]

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        try:
            _validate_codex_input(payload["input"])
        except ValidationError:
            # Reproduce the endpoint's rejection of Chat Completions content parts.
            return httpx.Response(
                400, json={"error": {"message": "Invalid Responses content", "type": "invalid_request_error"}}
            )
        events = [
            {"type": "response.output_text.delta", "delta": "BUB_READY"},
            {"type": "response.completed", "response": {"id": "resp_test", "model": "gpt-5.5"}},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"), http_client=http_client)
        stream = await provider.acompletion(model="gpt-5.5", messages=messages, stream=True)
        answer = "".join([chunk.choices[0].delta.content or "" async for chunk in stream if chunk.choices])

    assert answer == "BUB_READY"


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
    yield SimpleNamespace(type="response.function_call_arguments.delta", output_index=0, delta='{"command":')
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
        '{"command":"pwd"}'
    )
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


async def _codex_custom_tool_response_events():
    yield SimpleNamespace(
        type="response.output_item.added",
        output_index=1,
        item=SimpleNamespace(type="custom_tool_call", id="ctc_1", call_id="call_1", name="bash", input=""),
    )
    yield SimpleNamespace(
        type="response.custom_tool_call_input.delta", item_id="ctc_1", call_id="call_1", delta='{"command":'
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
        '{"command":"pwd"}'
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
