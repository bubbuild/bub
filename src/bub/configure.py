from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

CONFIG_MAP: dict[str, list[type[BaseSettings]]] = {}
ROOT = ""

_global_config: dict[str, list[BaseSettings]] | None = None


class Settings(BaseSettings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del settings_cls  # unused
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)


def config[C: type[BaseSettings]](name: str = ROOT) -> Callable[[C], C]:
    """Decorator to register a config class for a plugin."""

    def decorator(cls: C) -> C:
        if name not in CONFIG_MAP:
            CONFIG_MAP[name] = []
        CONFIG_MAP[name].append(cls)
        return cls

    return decorator


def load(config_file: Path) -> dict[str, list[BaseSettings]]:
    """Load config from a file."""
    import yaml

    global _global_config
    if _global_config is not None:
        return _global_config

    this_data: dict[str, list[BaseSettings]] = {}

    config_data: dict[str, Any] = {}
    if config_file.exists():
        with config_file.open() as f:
            config_data = yaml.safe_load(f) or {}

    for name, config_classes in CONFIG_MAP.items():
        section_data = config_data if name == ROOT else config_data.get(name, {})
        for config_cls in config_classes:
            config_instance = config_cls.model_validate(section_data)
            this_data.setdefault(name, []).append(config_instance)

    _global_config = this_data
    return _global_config


def ensure_config[C: BaseSettings](config_cls: type[C]) -> C:
    """No-op function to ensure a config class is registered and can be imported."""
    if _global_config is None:
        raise RuntimeError("Config not loaded yet")
    for config_list in _global_config.values():
        for config in config_list:
            if isinstance(config, config_cls):
                return config
    raise ValueError(f"Config class {config_cls} not found in loaded config")
