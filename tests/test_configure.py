from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import bub.configure as configure
from bub.builtin.settings import AgentSettings
from bub.channels.telegram import TelegramSettings


def test_merge_recursively_combines_non_conflicting_dicts() -> None:
    base = {"model": "openai:gpt-5", "telegram": {"token": "token"}}

    result = configure.merge(
        base,
        {"telegram": {"allow_users": "1,2"}},
    )

    assert result is base
    assert result == {
        "model": "openai:gpt-5",
        "telegram": {
            "token": "token",
            "allow_users": "1,2",
        },
    }


def test_merge_overrides_conflicting_scalar_values() -> None:
    base = {"model": "openai:gpt-5"}

    result = configure.merge(base, {"model": "anthropic:claude-3-7-sonnet"})

    assert result is base
    assert base == {"model": "anthropic:claude-3-7-sonnet"}


def test_validate_checks_registered_config_sections() -> None:
    valid_data = {
        "model": "openai:gpt-5",
        "telegram": {"token": "123:abc"},
    }

    assert configure.validate(valid_data) == valid_data

    with pytest.raises(ValidationError):
        configure.validate({"max_steps": "not-an-int"})


def test_save_writes_yaml_and_refreshes_loaded_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yml"
    expected_token = "123:abc"  # noqa: S105

    with patch.dict(os.environ, {}, clear=True):
        previous_cwd = Path.cwd()
        os.chdir(tmp_path)
        configure.save(
            config_file,
            {
                "model": "openai:gpt-5",
                "telegram": {"token": expected_token},
            },
        )

        try:
            loaded = configure.load(config_file)

            assert loaded["model"] == "openai:gpt-5"
            assert loaded["telegram"]["token"] == expected_token
            assert configure.ensure_config(AgentSettings).model == "openai:gpt-5"
            assert configure.ensure_config(TelegramSettings).token == expected_token
        finally:
            os.chdir(previous_cwd)


def test_get_value_reads_registered_section_from_yaml(load_config) -> None:
    with patch.dict(os.environ, {}, clear=True):
        load_config(
            """
telegram:
  token: yaml-token
""".strip(),
        )

        assert configure.get_value("telegram.token") == "yaml-token"


def test_get_value_prefers_registered_env_over_yaml(load_config) -> None:
    load_config(
        """
telegram:
  token: yaml-token
""".strip(),
    )

    with patch.dict(os.environ, {"BUB_TELEGRAM_TOKEN": "env-token"}, clear=True):
        configure._global_config.clear()

        assert configure.get_value("telegram.token") == "env-token"


def test_get_value_descends_into_registered_dict_field(load_config) -> None:
    with patch.dict(os.environ, {}, clear=True):
        load_config(
            """
api_key:
  openai: sk-yaml
""".strip(),
        )

        assert configure.get_value("api_key") == {"openai": "sk-yaml"}
        assert configure.get_value("api_key.openai") == "sk-yaml"


def test_get_value_ignores_raw_unregistered_path(load_config) -> None:
    load_config(
        """
custom:
  nested:
    value: raw-value
""".strip(),
    )

    with pytest.raises(KeyError):
        configure.get_value("custom.nested.value")


def test_get_value_returns_default_for_missing_path() -> None:
    assert configure.get_value("missing.value", default="fallback") == "fallback"
