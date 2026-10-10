"""Interactive model connection setup and discovery."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import republic
import typer
from republic.errors import AuthenticationError, ProviderNotFoundError, UnsupportedFeatureError

from bub import configure, inquirer
from bub.builtin.settings import DEFAULT_MODEL, AgentSettings, CustomProvider

PROVIDERS = {
    "openrouter": "OpenRouter (hosted model gateway)",
    "openai": "OpenAI (official API)",
    "openai-compatible": "OpenAI-compatible (custom URL / local server)",
    "anthropic": "Anthropic (Claude API)",
    "google": "Google Gemini (native API)",
    "codex": "Codex (ChatGPT plan login)",
    "azure-openai": "Azure OpenAI (Azure endpoint)",
    "ollama": "Ollama (local server)",
    "mistral": "Mistral AI",
    "deepseek": "DeepSeek",
    "magpie": "Magpie (local model gateway)",
    "custom": "Other registered Republic provider",
}
OPENAI_BASE = "https://api.openai.com/v1"
CONNECTION_TIMEOUT = 10
MANUAL_MODEL = "Enter a model ID manually"
EDIT_CONNECTION = "Edit URL / API key"
RETRY_CONNECTION = "Retry connection"


def _default_base(provider: str) -> str:
    try:
        client = republic.get_provider(provider, api_key="display-only", api_base="", env_prefix="BUB_DISPLAY_ONLY")
    except (AuthenticationError, ProviderNotFoundError, ValueError):
        return ""
    return client.api_base


def _endpoint(provider: str, api_base: str | None) -> str:
    """Resolve a URL for display and comparison, without changing client options."""
    return (api_base or _default_base(provider)).rstrip("/")


def _required_text(message: str, default: str = "") -> str:
    while True:
        value = inquirer.ask_text(message, default=default).strip()
        if value:
            return value
        typer.secho("Please enter a value.", fg="yellow")


def _ask_base(default: str, *, required: bool) -> str:
    typer.echo("Enter the API base URL, including /v1 if required; omit /chat/completions or /models.")
    if not required:
        typer.echo("Leave the URL blank to use the provider's default endpoint.")
    while True:
        value = inquirer.ask_text("API base URL", default=default).strip().rstrip("/")
        if not value and not required:
            return ""
        try:
            parsed = urlsplit(value)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.port != 0
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            valid = valid and not any(char.isspace() for char in value)
        except ValueError:
            valid = False
        if valid:
            return value
        typer.secho("Enter an http:// or https:// API base URL, without credentials, query or fragment.", fg="yellow")


async def _discover_models(provider: str, **client_args: Any) -> list[str]:
    async with asyncio.timeout(CONNECTION_TIMEOUT), republic.get_provider(provider, **client_args) as client:
        models = await client.list_models()
        return sorted({model.id.strip() for model in models if isinstance(model.id, str) and model.id.strip()})


def discover_models(provider: str, **client_args: Any) -> list[str]:
    """Fetch model IDs without generating tokens."""
    return asyncio.run(_discover_models(provider, **client_args))


def _connection_error(exc: Exception) -> str:
    # Provider errors may contain request URLs, response bodies or credentials.
    if isinstance(exc, AuthenticationError):
        return "Authentication failed or API key missing. Check the key and its permissions."
    if isinstance(exc, TimeoutError):
        return f"Connection timed out after {CONNECTION_TIMEOUT} seconds. Check the URL and network."
    if isinstance(exc, NotImplementedError | UnsupportedFeatureError | ProviderNotFoundError):
        return "Model discovery is unavailable for this provider. You can enter a model ID manually."
    status = getattr(exc, "status_code", None)
    if status in {401, 403}:
        return f"Authentication rejected (HTTP {status}). Check the API key and its permissions."
    if status == 404:
        return "Models endpoint not found (HTTP 404). Check the base URL, or enter a model ID manually."
    return "Could not fetch models. Check the URL, API key and network, or enter a model ID manually."


def _choose_model(models: list[str], default: str) -> str:
    if models:
        typer.echo("Choose a chat model with tool support. Model calls have not been tested.")
        selected = inquirer.ask_fuzzy(
            "LLM model (type to search)",
            choices=[*models, MANUAL_MODEL],
            default=default if default in models else models[0],
        )
        if selected != MANUAL_MODEL:
            return selected
    return _required_text("LLM model", default=default)


def _connection_config(
    current_config: dict[str, object], provider: str, api_base: str, api_key: str, api_format: str | None
) -> dict[str, object]:
    if isinstance(named := current_config.get("providers"), dict) and provider in named:
        return {"providers": {provider: {"api_base": api_base or None, "api_key": api_key or None}}}
    config: dict[str, object] = {}
    if api_format is not None:
        config["client_args"] = {**AgentSettings.model_validate(current_config).client_args, "api_format": api_format}
    for name, value in (("api_base", api_base), ("api_key", api_key)):
        existing = current_config.get(name)
        if isinstance(existing, dict):
            # A newly inserted empty value would mask provider-specific env vars.
            if value or provider in existing:
                config[name] = {provider: value}
        elif value or name in current_config:
            config[name] = value or None
    return config


def _select_connection(
    current_config: dict[str, object], current_provider: str, current_endpoint: str
) -> tuple[str, str, str, str | None]:
    choice = (
        "openai-compatible" if current_provider == "openai" and current_endpoint != OPENAI_BASE else current_provider
    )
    choices = dict(PROVIDERS)
    choices.setdefault(choice, choice)
    for registered in republic.all_providers():
        if registered != "typesafe":
            choices.setdefault(registered, registered)
    selected = inquirer.ask_fuzzy("LLM provider", choices=list(choices.values()), default=choices[choice])
    choice = next((name for name, label in choices.items() if label == selected), selected)
    provider = "openai" if choice == "openai-compatible" else choice
    if choice == "custom":
        provider = _required_text("Custom provider")

    # Prompt defaults come only from explicit configuration, never environment secrets.
    # Scalar credentials belong to the current provider; maps can configure several.
    explicit = {
        name: value
        for name in ("api_base", "api_key")
        if isinstance(value := current_config.get(name), dict) or provider == current_provider
    }
    stored = AgentSettings.model_construct(api_base=explicit.get("api_base"), api_key=explicit.get("api_key"))
    api_base = AgentSettings._provider_value(stored.api_base, provider) or ""
    api_key = AgentSettings._provider_value(stored.api_key, provider) or ""
    if isinstance(named := current_config.get("providers"), dict) and provider in named:
        custom = CustomProvider.model_validate(named[provider])
        api_base = custom.api_base or api_base
        api_key = custom.api_key or api_key
    previous_endpoint = _endpoint(provider, api_base)
    if choice == "openai" and previous_endpoint != OPENAI_BASE:
        api_base = OPENAI_BASE
    if endpoint := _endpoint(provider, api_base):
        typer.echo(f"API endpoint: {endpoint}" if api_base else f"Default API endpoint: {endpoint}")
    if (
        choice in {"openai-compatible", "custom"}
        or provider in {"azure-openai", "ollama"}
        or (choice != "openai" and api_base and _endpoint(provider, api_base) != _default_base(provider).rstrip("/"))
    ):
        api_base = _ask_base(api_base, required=choice == "openai-compatible" or provider == "azure-openai")
    if _endpoint(provider, api_base) != previous_endpoint:
        api_key = ""
    return provider, api_base, api_key, "chat" if choice == "openai-compatible" else None


def _ask_key(api_key: str) -> str:
    prompt = "API key (Enter to keep current key)" if api_key else "API key (optional)"
    api_key = inquirer.ask_secret(prompt).strip() or api_key
    return api_key


def _configure_connection(
    current_config: dict[str, object],
    provider: str,
    api_base: str,
    api_key: str,
    model_default: str,
    api_format: str | None,
) -> dict[str, object]:
    action = ""
    models: list[str] = []
    settings = AgentSettings.model_validate(current_config)
    provider_type = settings.providers[provider].type if provider in settings.providers else provider
    typer.echo("Leave the API key blank to keep the current key or use environment credentials.")
    while action != MANUAL_MODEL:
        if action == EDIT_CONNECTION:
            previous_endpoint = _endpoint(provider, api_base)
            api_base = _ask_base(api_base, required=bool(api_base))
            if _endpoint(provider, api_base) != previous_endpoint:
                api_key = model_default = ""
        if action != RETRY_CONNECTION:
            api_key = _ask_key(api_key) if provider_type != "codex" else ""
            config = _connection_config(current_config, provider, api_base, api_key, api_format)
            settings = AgentSettings.model_validate(configure.merge({}, current_config, config))
            client_args = settings.model_client_kwargs(provider)
            provider_type = settings.providers[provider].type if provider in settings.providers else provider
            if (client_args["api_base"] or "", client_args["api_key"] or "") != (api_base, api_key):
                typer.echo("BUB_* environment settings override this connection's URL or API key.")
        if provider_type == "codex":
            typer.echo("Using Codex account login. Model discovery is unavailable; enter a model ID manually.")
            break
        typer.echo("Checking connection and fetching models...")
        try:
            models = discover_models(provider_type, **client_args)
        except (NotImplementedError, UnsupportedFeatureError, ProviderNotFoundError) as exc:
            typer.secho(_connection_error(exc), fg="yellow")
            break
        except Exception as exc:
            typer.secho(_connection_error(exc), fg="yellow")
            action = inquirer.ask_select(
                "Connection check failed",
                choices=[EDIT_CONNECTION, RETRY_CONNECTION, MANUAL_MODEL],
                default=EDIT_CONNECTION,
            )
        else:
            typer.echo(f"Models endpoint reachable: found {len(models)} models.")
            break
    config["model"] = f"{provider}:{_choose_model(models, model_default)}"
    return config


def collect_model_config(current_config: dict[str, object]) -> dict[str, object]:
    settings = AgentSettings.model_validate(current_config)
    current_provider, separator, model_default = settings.model.partition(":")
    if not separator:
        current_provider, _, fallback = DEFAULT_MODEL.partition(":")
        model_default = settings.model.strip() or fallback
    current_provider = current_provider.lower()
    current_endpoint = _endpoint(current_provider, settings.model_client_kwargs(current_provider)["api_base"])
    provider, api_base, api_key, api_format = _select_connection(current_config, current_provider, current_endpoint)
    if (provider, _endpoint(provider, api_base)) != (current_provider, current_endpoint):
        model_default = ""
    return _configure_connection(current_config, provider, api_base, api_key, model_default, api_format)
