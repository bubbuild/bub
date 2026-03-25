from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bub.builtin.settings import AgentSettings, load_settings


def _settings_with_env(env: dict[str, str]) -> AgentSettings:
    with patch.dict("os.environ", env, clear=True):
        return AgentSettings()


def _write_config(home: Path, content: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yml").write_text(content, encoding="utf-8")


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


def test_settings_no_keys_returns_none() -> None:
    settings = _settings_with_env({})

    assert settings.api_key is None
    assert settings.api_base is None


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


def test_settings_load_values_from_yaml(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """
model: openai:gpt-5
fallback_models:
  - openai:gpt-4o-mini
max_steps: 77
api_key:
  openai: sk-yaml
api_base:
  openai: https://api.openai.com
""".strip(),
    )

    with patch.dict("os.environ", {"BUB_HOME": str(tmp_path)}, clear=True):
        settings = AgentSettings()

    assert settings.model == "openai:gpt-5"
    assert settings.fallback_models == ["openai:gpt-4o-mini"]
    assert settings.max_steps == 77
    assert settings.api_key == {"openai": "sk-yaml"}
    assert settings.api_base == {"openai": "https://api.openai.com"}


def test_env_settings_override_yaml(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """
model: openai:gpt-5
api_key: sk-yaml
max_steps: 77
""".strip(),
    )

    with patch.dict(
        "os.environ",
        {
            "BUB_HOME": str(tmp_path),
            "BUB_MODEL": "anthropic:claude-3-7-sonnet",
            "BUB_API_KEY": "sk-env",
            "BUB_MAX_STEPS": "12",
        },
        clear=True,
    ):
        settings = AgentSettings()

    assert settings.model == "anthropic:claude-3-7-sonnet"
    assert settings.api_key == "sk-env"
    assert settings.max_steps == 12


def test_load_settings_reads_yaml_from_bub_home(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """
model: openrouter:qwen/qwen3-coder-next
api_format: responses
""".strip(),
    )

    load_settings.cache_clear()
    try:
        with patch.dict("os.environ", {"BUB_HOME": str(tmp_path)}, clear=True):
            settings = load_settings()
    finally:
        load_settings.cache_clear()

    assert settings.model == "openrouter:qwen/qwen3-coder-next"
    assert settings.api_format == "responses"
