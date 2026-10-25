from __future__ import annotations

import os
import pathlib
import re
import sys
from typing import TYPE_CHECKING, Any

import republic
from pydantic import BaseModel, Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from bub import Settings, __version__, config, ensure_config
from bub.builtin.model import BubModel, ModelCandidate
from bub.core.commands import validate_command_prefix

if TYPE_CHECKING:
    from bub.core.tape import Tape
    from bub.core.tools import ToolContext

DEFAULT_MODEL = "openrouter:openrouter/free"
DEFAULT_MAX_TOKENS = 16384


def _provider_env_name(provider: str) -> str:
    return provider.upper().replace("-", "_")


USER_AGENT = f"bub/{__version__}"
"""Sent to model providers instead of Republic's default."""


class CustomProvider(BaseModel):
    """A named endpoint that speaks a registered Republic provider API."""

    type: str
    api_key: str | None = None
    api_base: str | None = None

    @field_validator("type")
    @classmethod
    def registered_type(cls, value: str) -> str:
        provider = value.lower()
        if provider not in republic.all_providers():
            raise ValueError(f"Unknown Republic provider type: {value!r}")
        return provider


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
        env_names = {_provider_env_name(name): name for name in republic.all_providers()}
        setting_regex = re.compile(rf"^BUB_(.+)_{setting_name.upper()}$")
        result: dict[str, str] = {}
        for key, value in os.environ.items():
            if match := setting_regex.match(key):
                result[env_names.get(match.group(1), match.group(1).lower())] = value
        return result


@config()
class AgentSettings(Settings):
    """Configuration settings for the Agent."""

    model_config = SettingsConfigDict(env_prefix="BUB_", env_parse_none_str="null", extra="ignore")
    model: str = DEFAULT_MODEL
    command_prefix: str = ","
    fallback_models: list[str] | None = None
    api_key: str | dict[str, str] | None = None
    api_base: str | dict[str, str] | None = None
    providers: dict[str, CustomProvider] = Field(default_factory=dict)
    max_steps: int = Field(default=sys.maxsize, gt=0)
    max_tokens: int = DEFAULT_MAX_TOKENS
    model_timeout_seconds: int | None = None
    client_args: dict[str, Any] = Field(default_factory=dict)
    completion_args: dict[str, Any] = Field(default_factory=dict)
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

    @field_validator("client_args", "completion_args", mode="before")
    @classmethod
    def default_dict_args(cls, value: Any) -> Any:
        return {} if value is None else value

    @field_validator("command_prefix")
    @classmethod
    def valid_command_prefix(cls, value: str) -> str:
        return validate_command_prefix(value)

    def chat_model(self, model: str | None = None, *, reasoning_effort: str | None = None) -> BubModel:
        """Build the Republic model for ``model`` (the configured model by default).

        Fallback models apply only to the configured model. ``reasoning_effort``
        overrides the one in ``completion_args``.
        """
        options = dict(self.completion_args)
        if reasoning_effort is not None:
            options["reasoning_effort"] = reasoning_effort
        return BubModel(
            model or self.model,
            fallbacks=(self.fallback_models or []) if (model or self.model) == self.model else (),
            providers={name: provider.type for name, provider in self.providers.items()},
            client_options=self.model_client_kwargs,
            options=options,
            max_tokens=self.max_tokens,
            timeout=self.model_timeout_seconds,
        )

    def model_candidates(self, model: str) -> list[ModelCandidate]:
        return self.chat_model().candidates(model)

    def model_providers(self, current_model: str | None = None) -> list[str]:
        """Providers to offer models from, ``current_model``'s first.

        These are the providers configured explicitly: of the model and fallbacks, custom
        providers, and per-provider ``api_key`` or ``api_base`` entries.
        """
        specs = [current_model, self.model, *(self.fallback_models or [])]
        providers = [spec.partition(":")[0] for spec in specs if spec]
        providers.extend(self.providers)
        for value in (self.api_key, self.api_base):
            if isinstance(value, dict):
                providers.extend(name for name, setting in value.items() if setting)
        return list(dict.fromkeys(name if name in self.providers else name.lower() for name in providers))

    def model_client_kwargs(self, provider: str) -> dict[str, Any]:
        custom = self.providers.get(provider)
        provider = provider if custom else provider.lower()
        options = dict(self.client_args)
        # Headers from client_args, including a User-Agent, take precedence.
        options["headers"] = {"User-Agent": USER_AGENT, **(options.get("headers") or {})}
        prefix = _provider_env_name(provider)
        options.setdefault("env_prefix", f"BUB_{prefix}")
        return {
            **options,
            "api_key": (custom and custom.api_key) or self._credential_value(self.api_key, provider, "api_key", prefix),
            "api_base": (custom and custom.api_base)
            or self._credential_value(self.api_base, provider, "api_base", prefix),
        }

    @classmethod
    def _credential_value(
        cls, value: str | dict[str, str] | None, provider: str, setting: str, prefix: str
    ) -> str | None:
        explicit = cls._provider_value(value, provider)
        return explicit or os.getenv(f"{prefix}_{setting.upper()}", explicit)

    @staticmethod
    def _provider_value(value: str | dict[str, str] | None, provider: str) -> str | None:
        return value.get(provider) if isinstance(value, dict) else value

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


# Per-session settings persisted as switch events on the session tape.

SESSION_SETTINGS = ("model", "reasoning_effort", "code_mode")


def _switch_event(key: str) -> str:
    return f"{key}_switch"


async def set_session_setting(context: ToolContext, key: str, value: Any) -> None:
    """Apply a session setting to the current state and record it on the session tape.

    The event is merged back with the turn, so ``load_session_settings`` restores the
    value on the next turn and after restarts.
    """
    if key not in SESSION_SETTINGS:
        raise ValueError(f"unknown session setting: {key!r}")
    context.state[key] = value
    await context.tape.append_event(_switch_event(key), {key: value})


async def load_session_settings(tape: Tape) -> dict[str, Any]:
    """Return the latest recorded value of each session setting; empty values clear it."""
    keys = {_switch_event(key): key for key in SESSION_SETTINGS}
    settings: dict[str, Any] = {}
    for entry in await tape.store.fetch_all(tape.query().kinds("event")):
        key = keys.get(entry.payload.get("name", ""))
        data = entry.payload.get("data")
        if key is None or not isinstance(data, dict) or key not in data:
            continue
        if data[key] is None or data[key] == "":
            settings.pop(key, None)
        else:
            settings[key] = data[key]
    return settings
