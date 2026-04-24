from __future__ import annotations

import os
from unittest.mock import patch

from bub.builtin.settings import DEFAULT_MODEL, AgentSettings, load_settings


def _settings_with_env(env: dict[str, str]) -> AgentSettings:
    with patch.dict("os.environ", env, clear=True):
        return AgentSettings()


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
    assert settings.client_args is None


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
  extra_headers:
    HTTP-Referer: https://openclaw.ai
    X-Title: OpenClaw
""".strip(),
        )

        settings = load_settings()

    assert settings.model == "openai:gpt-5"
    assert settings.fallback_models == ["openai:gpt-4o-mini"]
    assert settings.max_steps == 77
    assert settings.api_key == {"openai": "sk-yaml"}
    assert settings.api_base == {"openai": "https://api.openai.com"}
    assert settings.client_args == {
        "extra_headers": {"HTTP-Referer": "https://openclaw.ai", "X-Title": "OpenClaw"},
    }


def test_env_settings_override_yaml(load_config) -> None:
    config = """
model: openai:gpt-5
api_key: sk-yaml
max_steps: 77
client_args:
  extra_headers:
    HTTP-Referer: https://yaml.example
    X-Title: YAML App
""".strip()

    with patch.dict(
        "os.environ",
        {
            "BUB_MODEL": "anthropic:claude-3-7-sonnet",
            "BUB_API_KEY": "sk-env",
            "BUB_CLIENT_ARGS": '{"extra_headers":{"HTTP-Referer":"https://env.example","X-Title":"Env App"}}',
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
        "extra_headers": {"HTTP-Referer": "https://env.example", "X-Title": "Env App"},
    }


def test_settings_client_args_can_be_disabled() -> None:
    settings = _settings_with_env({"BUB_CLIENT_ARGS": "null"})

    assert settings.client_args is None


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
api_format: responses
""".strip(),
        )

        settings = load_settings()

    assert settings.model == "openrouter:openrouter/free"
    assert settings.api_format == "responses"
