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
