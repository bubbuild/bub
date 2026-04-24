from __future__ import annotations

import os
import pathlib
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub import Settings, config, ensure_config

DEFAULT_MODEL = "openrouter:openrouter/free"
DEFAULT_MAX_TOKENS = 1024


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


@config()
class AgentSettings(Settings):
    """Configuration settings for the Agent."""

    model_config = SettingsConfigDict(env_prefix="BUB_", env_parse_none_str="null", extra="ignore")
    model: str = DEFAULT_MODEL
    fallback_models: list[str] | None = None
    api_key: str | dict[str, str] | None = Field(default_factory=provider_specific("api_key"))
    api_base: str | dict[str, str] | None = Field(default_factory=provider_specific("api_base"))
    api_format: Literal["completion", "responses", "messages"] = "completion"
    max_steps: int = 50
    max_tokens: int = DEFAULT_MAX_TOKENS
    model_timeout_seconds: int | None = None
    client_args: dict[str, Any] | None = None
    verbose: int = Field(default=0, description="Verbosity level for logging. Higher means more verbose.", ge=0, le=2)

    @property
    def home(self) -> pathlib.Path:
        import warnings

        import bub

        warnings.warn(
            "Using the 'home' property from AgentSettings is deprecated. Please use 'bub.home' instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        return bub.home


def load_settings() -> AgentSettings:
    return ensure_config(AgentSettings)
