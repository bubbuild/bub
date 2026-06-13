from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any

from any_llm import AnyLLM
from any_llm.constants import LLMProvider
from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from bub import Settings, config, ensure_config

DEFAULT_MODEL = "openrouter:openrouter/free"
DEFAULT_MAX_TOKENS = 16384


@dataclass(frozen=True)
class ModelCandidate:
    provider: LLMProvider
    model_id: str
    name: str


class ProviderSpecificEnvSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name, setting_name in (("api_key", "api_key"), ("api_base", "api_base")):
            values = self._provider_specific(setting_name)
            if values:
                result[field_name] = values
        return result

    @staticmethod
    def _provider_specific(setting_name: str) -> dict[str, str]:
        setting_regex = re.compile(rf"^BUB_(.+)_{setting_name.upper()}$")
        result: dict[str, str] = {}
        for key, value in os.environ.items():
            if match := setting_regex.match(key):
                result[match.group(1).lower()] = value
        return result


@config()
class AgentSettings(Settings):
    """Configuration settings for the Agent."""

    model_config = SettingsConfigDict(env_prefix="BUB_", env_parse_none_str="null", extra="ignore")
    model: str = DEFAULT_MODEL
    fallback_models: list[str] | None = None
    api_key: str | dict[str, str] | None = None
    api_base: str | dict[str, str] | None = None
    max_steps: int = 50
    max_tokens: int = DEFAULT_MAX_TOKENS
    model_timeout_seconds: int | None = None
    client_args: dict[str, Any] = Field(default_factory=dict)
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
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            ProviderSpecificEnvSource(settings_cls),
            file_secret_settings,
        )

    @field_validator("client_args", mode="before")
    @classmethod
    def default_client_args(cls, value: Any) -> Any:
        return {} if value is None else value

    def model_candidates(self, model: str) -> list[ModelCandidate]:
        candidate_names = [model]
        if model == self.model:
            candidate_names.extend(self.fallback_models or [])

        candidates: list[ModelCandidate] = []
        for candidate in candidate_names:
            provider, model_id = AnyLLM.split_model_provider(candidate)
            candidates.append(ModelCandidate(provider=provider, model_id=model_id, name=candidate))
        return candidates

    def model_client_kwargs(self, provider: LLMProvider) -> dict[str, Any]:
        return {
            **self.client_args,
            "api_key": self._provider_value(self.api_key, provider),
            "api_base": self._provider_value(self.api_base, provider),
        }

    @staticmethod
    def _provider_value(value: str | dict[str, str] | None, provider: LLMProvider) -> str | None:
        if isinstance(value, dict):
            return value.get(provider.value)
        return value

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
