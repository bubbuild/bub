"""Bub's model client: fallbacks, custom providers and per-provider credentials from settings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import republic
from loguru import logger

from bub.tracing import current_span, event


@dataclass(frozen=True)
class ModelCandidate:
    provider: str
    model_id: str
    name: str
    provider_name: str | None = None


ClientOptions = Mapping[str, Any] | Callable[[str], Mapping[str, Any]]
"""Republic client options, or a function returning them for a provider name."""


class BubModel:
    """A ``provider:model`` spec with Bub's client policy, usable as the agent loop's model.

    Args:
        name: Default ``provider:model`` spec. Hooks may redirect a call to another spec.
        fallbacks: Specs tried in order when opening the default model fails.
        providers: Custom provider names mapped to the registered Republic provider type they speak.
        client_options: Options for ``republic.get_model``, or a function of the provider name
            (the custom name when one is used) returning them.
        options: Generation options (``republic.ChatOptions``) sent with every call.
        max_tokens: Default output token limit, unless a call sets one.
        timeout: Seconds allowed for one complete call, streaming included.
    """

    def __init__(
        self,
        name: str,
        *,
        fallbacks: Sequence[str] = (),
        providers: Mapping[str, str] | None = None,
        client_options: ClientOptions | None = None,
        options: Mapping[str, Any] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.name = name
        self.fallbacks = tuple(fallbacks)
        self.providers = dict(providers or {})
        self.client_options = client_options
        self.options = dict(options or {})
        self.max_tokens = max_tokens
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    @property
    def spec(self) -> str:
        return self.name

    def candidates(self, model: str | None = None) -> list[ModelCandidate]:
        """Resolve ``model`` (the default when omitted) and, for the default, its fallbacks."""
        model = model or self.name
        candidate_names = [model]
        if model == self.name:
            candidate_names.extend(self.fallbacks)

        candidates: list[ModelCandidate] = []
        for candidate in candidate_names:
            provider, separator, model_id = candidate.partition(":")
            if not separator or not provider or not model_id:
                raise ValueError(f"Expected a provider:model identifier, got {candidate!r}")
            if custom_type := self.providers.get(provider):
                candidates.append(ModelCandidate(custom_type, model_id, candidate, provider))
                continue
            provider = provider.lower()
            candidates.append(ModelCandidate(provider=provider, model_id=model_id, name=f"{provider}:{model_id}"))
        return candidates

    def client_kwargs(self, provider: str) -> dict[str, Any]:
        """Return Republic client options for a provider name."""
        if callable(self.client_options):
            return dict(self.client_options(provider))
        return dict(self.client_options or {})

    def chat_options(self, provider: str, options: Mapping[str, Any]) -> dict[str, Any]:
        """Merge this model's generation options with a call's for ``provider``."""
        merged = {**self.options, **options}
        if provider == "anthropic":
            merged["extra_body"] = {"cache_control": {"type": "ephemeral"}} | merged.get("extra_body", {})
        max_tokens = options.get("max_tokens", self.max_tokens)
        merged.pop("max_tokens", None)
        if provider != "codex" and max_tokens is not None:
            merged["max_tokens"] = max_tokens
        return merged

    @asynccontextmanager
    async def open_stream(
        self, messages: list[republic.Message], *, spec: str, options: Mapping[str, Any]
    ) -> AsyncIterator[tuple[str, republic.Stream[Any]]]:
        """Open a streaming completion, falling back to later candidates if opening fails.

        Fallback covers client creation and the request; errors raised while consuming the
        stream are not retried. ``timeout`` covers the whole call.
        """
        async with asyncio.timeout(self.timeout):
            completion_error: Exception | None = None
            candidates = self.candidates(spec)
            for index, candidate in enumerate(candidates):
                async with AsyncExitStack() as stack:
                    try:
                        chat_model = await stack.enter_async_context(
                            republic.get_model(
                                f"{candidate.provider}:{candidate.model_id}",
                                **self.client_kwargs(candidate.provider_name or candidate.provider),
                            )
                        )
                        if span := current_span():
                            span.rename(f"chat {candidate.model_id}")
                            span.set(**{
                                "gen_ai.provider.name": candidate.provider,
                                "gen_ai.request.model": candidate.model_id,
                            })
                        stream = await stack.enter_async_context(
                            chat_model.stream(messages, **self.chat_options(candidate.provider, options))
                        )
                    except Exception as exc:
                        event("bub.model.attempt_failed", model=candidate.name, error=type(exc).__name__)
                        if completion_error is None:
                            completion_error = exc
                        if index == len(candidates) - 1:
                            raise completion_error from None
                        logger.warning(
                            "model candidate failed; trying fallback model={} error={}",
                            candidate.name,
                            type(exc).__name__,
                        )
                    else:
                        yield candidate.provider_name or candidate.provider, stream
                        return

            raise RuntimeError("no model candidates available")
