from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

CONFIG_MAP: dict[str, list[type[BaseSettings]]] = {}
ROOT = ""
MISSING = object()

_global_config: dict[str, list[BaseSettings]] = {}
_config_data: dict[str, Any] = {}


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
        cls.__config_name__ = name  # type: ignore[attr-defined]
        if name not in CONFIG_MAP:
            CONFIG_MAP[name] = []
        CONFIG_MAP[name].append(cls)
        return cls

    return decorator


def load(config_file: Path) -> dict[str, Any]:
    """Load config from a file."""
    import yaml

    _global_config.clear()
    _config_data.clear()
    if config_file.exists():
        with config_file.open() as f:
            _config_data.update(yaml.safe_load(f) or {})
    return _config_data


def merge(base: dict[str, Any], *updates: dict[str, Any]) -> dict[str, Any]:
    """Update base in place with config updates, preferring incoming values on conflict."""

    for update in updates:
        _merge_into(base, update, path=())
    return base


def validate(config_data: dict[str, Any]) -> dict[str, Any]:
    """Validate config data against all registered config classes."""

    for section, config_classes in CONFIG_MAP.items():
        section_data = config_data if section == ROOT else config_data.get(section, {})
        for config_cls in config_classes:
            config_cls.model_validate(section_data)
    return config_data


def save(config_file: Path, config_data: dict[str, Any]) -> None:
    """Validate and persist config data to a YAML file."""
    import yaml

    validated = validate(config_data)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with config_file.open("w", encoding="utf-8") as f:
        yaml.safe_dump(validated, f, sort_keys=False)


def ensure_config[C: BaseSettings](config_cls: type[C]) -> C:
    """No-op function to ensure a config class is registered and can be imported."""
    section = getattr(config_cls, "__config_name__", ROOT)
    if section not in CONFIG_MAP:
        raise ValueError(f"No config registered for section '{section}'")

    instances = _global_config.setdefault(section, [])
    for instance in instances:
        if isinstance(instance, config_cls):
            return instance

    section_data = _config_data.get(section, {}) if section != ROOT else _config_data
    instance = config_cls.model_validate(section_data)
    instances.append(instance)
    return instance


def get_value(path: str, default: Any = MISSING) -> Any:
    """Get a loaded config value by dotted path, preserving registered settings behavior."""

    parts = [part for part in path.split(".") if part]
    if not parts:
        raise ValueError("config path must not be empty")

    value = _lookup_registered_config(parts)
    if value is not MISSING:
        return value

    if default is not MISSING:
        return default
    raise KeyError(path)


def _copy_dict(data: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            copied[key] = _copy_dict(value)
        else:
            copied[key] = value
    return copied


def _lookup_registered_config(parts: list[str]) -> Any:
    section, *subpath = parts
    if section in CONFIG_MAP and section != ROOT:
        for config_cls in CONFIG_MAP[section]:
            value = _lookup_path(ensure_config(config_cls), subpath)
            if value is not MISSING:
                return value

    for config_cls in CONFIG_MAP.get(ROOT, []):
        value = _lookup_path(ensure_config(config_cls), parts)
        if value is not MISSING:
            return value

    return MISSING


def _lookup_path(value: Any, parts: list[str]) -> Any:
    current = value
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return MISSING
            current = current[part]
            continue
        if not hasattr(current, part):
            return MISSING
        current = getattr(current, part)
    return current


def _merge_into(target: dict[str, Any], incoming: dict[str, Any], path: tuple[str, ...]) -> None:
    for key, value in incoming.items():
        existing = target.get(key)
        if key not in target:
            target[key] = _copy_dict(value) if isinstance(value, dict) else value
            continue
        if isinstance(existing, dict) and isinstance(value, dict):
            _merge_into(existing, value, path=(*path, key))
            continue
        target[key] = _copy_dict(value) if isinstance(value, dict) else value
