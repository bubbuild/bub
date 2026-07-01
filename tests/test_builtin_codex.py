from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from any_llm.constants import LLMProvider
from any_llm.types.completion import ChatCompletion
from any_llm.types.responses import Response

from bub.builtin.auth import (
    OpenAICodexOAuthTokens,
    extract_openai_codex_account_id,
    load_openai_codex_oauth_tokens,
    openai_codex_oauth_resolver,
    save_openai_codex_oauth_tokens,
)
from bub.builtin.codex_provider import OpenaiCodexProvider, should_use_openai_codex_provider
from bub.builtin.model_runner import ModelRunner
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


def _response_payload(*, output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "model": "gpt-5-codex",
        "output": output,
        "parallel_tool_calls": False,
        "temperature": None,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 2,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 3,
        },
        "store": False,
    }


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


def test_codex_provider_converts_response_to_chat_completion() -> None:
    response = Response.model_validate(
        _response_payload(
            output=[
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "hello", "annotations": []}],
                }
            ]
        )
    )
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))

    completion = provider._response_to_completion(response, model="gpt-5-codex")

    assert isinstance(completion, ChatCompletion)
    assert completion.choices[0].message.content == "hello"
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage is not None
    assert completion.usage.prompt_tokens == 1
    assert completion.usage.completion_tokens == 2


def test_codex_provider_converts_function_call_to_chat_completion_tool_call() -> None:
    response = Response.model_validate(
        _response_payload(
            output=[
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "tool_name",
                    "arguments": '{"ok": true}',
                    "status": "completed",
                }
            ]
        )
    )
    provider = OpenaiCodexProvider(api_key=_jwt_with_account("acct_123"))

    completion = provider._response_to_completion(response, model="gpt-5-codex")

    assert completion.choices[0].finish_reason == "tool_calls"
    tool_calls = completion.choices[0].message.tool_calls
    assert tool_calls is not None
    assert tool_calls[0].id == "call_1"
    assert tool_calls[0].function.name == "tool_name"
    assert tool_calls[0].function.arguments == '{"ok": true}'
