from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import httpx2
import pytest
from pydantic import ValidationError

from bub.builtin.model_runner import ModelRunner
from bub.builtin.settings import DEFAULT_MODEL, AgentSettings, load_settings
from bub.builtin.spill import SpillSettings
from bub.configure import ensure_config
from bub.prompt import to_content
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.tape import Tape, TapeContext
from tests.model_fakes import ProviderService, chat_events, sse


def _settings_with_env(env: dict[str, str]) -> AgentSettings:
    with patch.dict("os.environ", env, clear=True):
        return AgentSettings()


@pytest.mark.parametrize("prefix", ["", "! a"])
def test_command_prefix_rejects_empty_or_whitespace(prefix: str) -> None:
    with pytest.raises(ValidationError, match="command_prefix"):
        _settings_with_env({"BUB_COMMAND_PREFIX": prefix})


def test_settings_single_api_key_and_base() -> None:
    settings = _settings_with_env({"BUB_API_KEY": "sk-test", "BUB_API_BASE": "https://api.example.com"})

    assert isinstance(settings.api_key, str)
    assert isinstance(settings.api_base, str)


def test_settings_per_provider_keys() -> None:
    settings = _settings_with_env({
        "BUB_OPENAI_API_KEY": "sk-openai",
        "BUB_OPENAI_API_BASE": "https://api.openai.com",
        "BUB_ANTHROPIC_API_KEY": "sk-anthropic",
    })

    assert isinstance(settings.api_key, dict)
    assert settings.api_key["openai"] == "sk-openai"
    assert settings.api_key["anthropic"] == "sk-anthropic"
    assert isinstance(settings.api_base, dict)
    assert settings.api_base["openai"] == "https://api.openai.com"


def test_settings_no_keys_return_none() -> None:
    settings = _settings_with_env({})

    assert settings.api_key is None
    assert settings.api_base is None
    assert settings.client_args == {}
    assert settings.completion_args == {}


def test_settings_provider_names_are_lowercased() -> None:
    settings = _settings_with_env({"BUB_OPENROUTER_API_KEY": "sk-or"})

    assert isinstance(settings.api_key, dict)
    assert "openrouter" in settings.api_key


def test_settings_mixed_single_key_with_per_provider_base() -> None:
    settings = _settings_with_env({
        "BUB_API_KEY": "sk-global",
        "BUB_OPENAI_API_BASE": "https://api.openai.com",
    })

    assert settings.api_key == "sk-global"
    assert isinstance(settings.api_base, dict)
    assert settings.api_base["openai"] == "https://api.openai.com"


def test_settings_load_values_from_yaml(load_config) -> None:
    with patch.dict(os.environ, {}, clear=True):
        load_config(
            """
model: openai:gpt-5
fallback_models:
  - openai:gpt-4o-mini
max_steps: 77
api_key:
  openai: sk-yaml
api_base:
  openai: https://api.openai.com
client_args:
  headers:
    HTTP-Referer: https://openclaw.ai
    X-Title: OpenClaw
completion_args:
  reasoning_effort: high
""".strip(),
        )

        settings = load_settings()

    assert settings.model == "openai:gpt-5"
    assert settings.fallback_models == ["openai:gpt-4o-mini"]
    assert settings.max_steps == 77
    assert settings.api_key == {"openai": "sk-yaml"}
    assert settings.api_base == {"openai": "https://api.openai.com"}
    assert settings.client_args == {
        "headers": {"HTTP-Referer": "https://openclaw.ai", "X-Title": "OpenClaw"},
    }
    assert settings.completion_args == {"reasoning_effort": "high"}


def test_env_settings_override_yaml(load_config) -> None:
    config = """
model: openai:gpt-5
api_key: sk-yaml
max_steps: 77
client_args:
  headers:
    HTTP-Referer: https://yaml.example
    X-Title: YAML App
""".strip()

    with patch.dict(
        "os.environ",
        {
            "BUB_MODEL": "anthropic:claude-3-7-sonnet",
            "BUB_API_KEY": "sk-env",
            "BUB_CLIENT_ARGS": '{"headers":{"HTTP-Referer":"https://env.example","X-Title":"Env App"}}',
            "BUB_COMPLETION_ARGS": '{"reasoning_effort":"medium"}',
            "BUB_MAX_STEPS": "12",
        },
        clear=True,
    ):
        load_config(config)
        settings = load_settings()

    assert settings.model == "anthropic:claude-3-7-sonnet"
    assert settings.api_key == "sk-env"
    assert settings.max_steps == 12
    assert settings.client_args == {
        "headers": {"HTTP-Referer": "https://env.example", "X-Title": "Env App"},
    }
    assert settings.completion_args == {"reasoning_effort": "medium"}


def test_settings_client_args_can_be_disabled() -> None:
    settings = _settings_with_env({"BUB_CLIENT_ARGS": "null", "BUB_COMPLETION_ARGS": "null"})

    assert settings.client_args == {}
    assert settings.completion_args == {}


def test_spill_sidecar_settings_can_be_configured_or_disabled() -> None:
    with patch.dict("os.environ", {"BUB_SPILL_THRESHOLD": "64"}, clear=True):
        assert SpillSettings().threshold == 64
    with patch.dict("os.environ", {"BUB_SPILL_THRESHOLD": "0"}, clear=True):
        assert SpillSettings().threshold == 0


def test_spill_sidecar_settings_load_from_the_plugin_section(load_config) -> None:
    load_config("spill:\n  threshold: 64")

    assert ensure_config(SpillSettings).threshold == 64


def test_load_settings_returns_defaults_without_loaded_config() -> None:
    with patch.dict(os.environ, {}, clear=True):
        settings = load_settings()

    assert settings.model == DEFAULT_MODEL
    assert settings.max_steps == AgentSettings.model_fields["max_steps"].default


def test_load_settings_returns_loaded_config(load_config) -> None:
    with patch.dict(os.environ, {}, clear=True):
        load_config(
            """
model: openrouter:openrouter/free
""".strip(),
        )

        settings = load_settings()

    assert settings.model == "openrouter:openrouter/free"


async def _run(settings: AgentSettings, tmp_path: Path) -> str:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext(anchor=None)).scoped("settings")
    events = [
        event
        async for event in ModelRunner(settings).run(
            tape=tape, model=settings.model, tools=[], system_prompt=None, prompt=to_content("Hello")
        )
    ]
    assert events[-1].data["ok"]
    return events[-1].data["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "acme"])
async def test_client_options_resolve_provider_names(
    provider: str, tmp_path: Path, provider_transport: ProviderService
) -> None:
    provider_transport.reply_chat()
    env = {
        f"BUB_{provider.upper()}_API_KEY": "environment-key",
        f"BUB_{provider.upper()}_API_BASE": "https://example.test/v1",
        "BUB_CLIENT_ARGS": '{"api_key": "ignored-key", "api_base": "https://ignored.test", "api_format": "chat"}',
    }
    if provider == "acme":
        env["BUB_PROVIDERS"] = '{"acme": {"type": "openai"}}'
    settings = _settings_with_env(env)
    settings.model = f"{provider}:model"
    assert await _run(settings, tmp_path) == "done"
    assert provider_transport.requests[0].url.host == "example.test"
    assert provider_transport.requests[0].headers["authorization"] == "Bearer environment-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,header", [("google", "x-goog-api-key"), ("azure-openai", "api-key"), ("openrouter", "authorization")]
)
@pytest.mark.parametrize("explicit_key", [False, True])
async def test_provider_requests_use_environment_or_explicit_credentials(
    provider: str, header: str, explicit_key: bool, tmp_path: Path, provider_service: ProviderService
) -> None:
    prefix = provider.upper().replace("-", "_")
    provider_service.reply(
        sse(
            [{"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}]
            if provider == "google"
            else chat_events()
        )
    )
    with patch.dict(
        os.environ,
        {f"{prefix}_API_KEY": "environment-key", f"{prefix}_API_BASE": "https://provider.test/v1"},
        clear=True,
    ):
        async with provider_service.client() as client:
            settings = AgentSettings(
                model=f"{provider}:model:variant",
                api_key={provider: "explicit-key"} if explicit_key else None,
                client_args={"http_client": client, "api_format": "gemini" if provider == "google" else "chat"},
            )
            assert await _run(settings, tmp_path) == "done"
    request = provider_service.requests[0]
    key = "explicit-key" if explicit_key else "environment-key"
    assert request.headers[header] == (f"Bearer {key}" if header == "authorization" else key)
    assert request.url.host == "provider.test"
    if provider == "google":
        assert "model:variant" in request.url.path
    else:
        assert provider_service.body()["model"] == "model:variant"


@pytest.mark.asyncio
async def test_provider_and_completion_extras_follow_republic_deep_merge(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(sse([{"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]}]))
    async with provider_service.client() as client:
        settings = AgentSettings(
            model="google:test",
            max_tokens=100,
            client_args={
                "http_client": client,
                "extra_body": {"generationConfig": {"maxOutputTokens": 1, "temperature": 0.4, "topK": 12}},
            },
            completion_args={"extra_body": {"generationConfig": {"temperature": 0.9, "topP": 0.8}}},
        )
        assert await _run(settings, tmp_path) == "done"
    assert provider_service.body()["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]
    assert provider_service.body()["generationConfig"] == {
        "maxOutputTokens": 1,
        "temperature": 0.9,
        "topK": 12,
        "topP": 0.8,
    }


@pytest.mark.asyncio
async def test_named_endpoint_and_builtin_fallback_use_separate_credentials(
    load_config, tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(httpx2.Response(503, json={"error": {"message": "unavailable"}}))
    provider_service.reply(sse(chat_events()))
    with patch.dict(os.environ, {}, clear=True):
        load_config("""
model: relay:qwen/kimi-k3
fallback_models:
  - openai:gpt-5
api_key:
  openai: openai-key
api_base:
  openai: https://openai.test/v1
providers:
  relay:
    type: openai
    api_base: https://relay.test/v1
    api_key: relay-key
""")
        async with provider_service.client() as client:
            settings = load_settings()
            settings.client_args = {"http_client": client, "api_format": "chat", "max_retries": 0}
            assert await _run(settings, tmp_path) == "done"
    assert [request.url.host for request in provider_service.requests] == ["relay.test", "openai.test"]
    assert [request.headers["authorization"] for request in provider_service.requests] == [
        "Bearer relay-key",
        "Bearer openai-key",
    ]
    assert [provider_service.body(index)["model"] for index in range(2)] == ["qwen/kimi-k3", "gpt-5"]


@pytest.mark.asyncio
async def test_named_endpoint_uses_its_environment_credentials(
    tmp_path: Path, provider_service: ProviderService
) -> None:
    provider_service.reply(
        sse([
            {"type": "message_start", "message": {"id": "test", "usage": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "done"}},
            {"type": "message_stop"},
        ])
    )
    with patch.dict(
        os.environ,
        {
            "BUB_MODEL": "relay:test",
            "BUB_PROVIDERS": '{"relay": {"type": "anthropic", "api_base": "https://relay.test"}}',
            "BUB_RELAY_API_KEY": "environment-key",
        },
        clear=True,
    ):
        async with provider_service.client() as client:
            settings = AgentSettings(client_args={"http_client": client})
            assert await _run(settings, tmp_path) == "done"
    assert provider_service.requests[0].url.host == "relay.test"
    assert provider_service.requests[0].headers["x-api-key"] == "environment-key"


def test_custom_provider_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError, match="providers"):
        _settings_with_env({"BUB_PROVIDERS": '{"relay": {"type": "nope"}}'})


@pytest.mark.parametrize("provider", ["azure-openai", "github-copilot"])
def test_hyphenated_provider_names_resolve_environment_credentials(provider: str) -> None:
    prefix = provider.upper().replace("-", "_")
    settings = _settings_with_env({f"BUB_{prefix}_API_KEY": "provider-key"})
    assert settings.api_key == {provider: "provider-key"}


def test_model_clients_identify_as_bub() -> None:
    import republic

    import bub

    settings = AgentSettings(api_key="sk-test")
    kwargs = settings.model_client_kwargs("openai")
    provider = republic.get_provider("openai", **kwargs)

    assert provider.headers["User-Agent"] == f"bub/{bub.__version__}"
    custom = AgentSettings(api_key="sk-test", client_args={"headers": {"User-Agent": "mine", "X-Team": "a"}})
    assert custom.model_client_kwargs("openai")["headers"] == {"User-Agent": "mine", "X-Team": "a"}
