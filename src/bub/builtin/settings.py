from __future__ import annotations

import os
import pathlib
import re
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "openrouter:qwen/qwen3-coder-next"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_HOME = pathlib.Path.home() / ".bub"


class AgentSettings(BaseSettings):
    """Configuration settings for the Agent, loaded from environment variables with prefix BUB_ or from a .env file."""

    model_config = SettingsConfigDict(env_prefix="BUB_", env_parse_none_str="null", extra="ignore")

    home: pathlib.Path = Field(default=DEFAULT_HOME)

    model: str = DEFAULT_MODEL
    fallback_models: list[str] | None = None
    api_key: str | dict[str, str] | None = None
    api_base: str | dict[str, str] | None = None
    api_format: Literal["completion", "responses", "messages"] = "completion"
    max_steps: int = 50
    max_tokens: int = DEFAULT_MAX_TOKENS
    model_timeout_seconds: int | None = None
    verbose: int = Field(default=0, description="Verbosity level for logging. Higher means more verbose.", ge=0, le=2)

    @classmethod
    def from_env(cls) -> AgentSettings:
        from dotenv import dotenv_values

        key_regex = re.compile(r"^BUB_(.+)_API_KEY$")
        base_regex = re.compile(r"^BUB_(.+)_API_BASE$")

        loaded_env = dotenv_values(".env")
        loaded_env.update(os.environ)

        api_key: str | dict[str, str] | None = loaded_env.get("BUB_API_KEY")
        api_base: str | dict[str, str] | None = loaded_env.get("BUB_API_BASE")
        if api_key and api_base:
            return cls()

        if api_key is None:
            api_key = {}
        if api_base is None:
            api_base = {}

        for key, value in loaded_env.items():
            if value is None:
                continue
            if isinstance(api_key, dict) and (match := key_regex.match(key)):
                provider = match.group(1).lower()
                api_key[provider] = value
            if isinstance(api_base, dict) and (match := base_regex.match(key)):
                provider = match.group(1).lower()
                api_base[provider] = value

        return cls(api_key=api_key or None, api_base=api_base or None)
