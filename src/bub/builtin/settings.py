from __future__ import annotations

import os
import pathlib
import re
from collections.abc import Callable
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

DEFAULT_MODEL = "openrouter:qwen/qwen3-coder-next"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_HOME = pathlib.Path.home() / ".bub"
DEFAULT_CONFIG_FILE = DEFAULT_HOME / "config.yml"


def provider_specific(setting_name: str) -> Callable[[], dict[str, str] | None]:
    def default_factory() -> dict[str, str] | None:
        setting_regex = re.compile(rf"^BUB_(.+)_{setting_name.upper()}$")
        loaded_env = os.environ
        result: dict[str, str] = {}
        for key, value in loaded_env.items():
            if value is None:
                continue
            if match := setting_regex.match(key):
                provider = match.group(1).lower()
                result[provider] = value
        return result or None

    return default_factory


class AgentSettings(BaseSettings):
    """Configuration settings for the Agent."""

    model_config = SettingsConfigDict(env_prefix="BUB_", env_parse_none_str="null", extra="ignore")
    home: pathlib.Path = Field(default=DEFAULT_HOME)
    model: str = DEFAULT_MODEL
    fallback_models: list[str] | None = None
    api_key: str | dict[str, str] | None = Field(default_factory=provider_specific("api_key"))
    api_base: str | dict[str, str] | None = Field(default_factory=provider_specific("api_base"))
    api_format: Literal["completion", "responses", "messages"] = "completion"
    max_steps: int = 50
    max_tokens: int = DEFAULT_MAX_TOKENS
    model_timeout_seconds: int | None = None
    verbose: int = Field(default=0, description="Verbosity level for logging. Higher means more verbose.", ge=0, le=2)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        home = os.getenv("BUB_HOME", str(DEFAULT_HOME))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=pathlib.Path(home) / "config.yml"),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def load_settings() -> AgentSettings:
    return AgentSettings()
