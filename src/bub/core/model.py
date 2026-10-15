"""Models the agent loop calls: Republic chat models, or clients streaming like them."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

import republic

CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long|tokens? > \d+ maximum",
    re.IGNORECASE,
)


def is_context_length_error(error_msg: str) -> bool:
    """Check whether an error message indicates a context-length / prompt-too-long failure."""
    return bool(CONTEXT_LENGTH_PATTERNS.search(error_msg))


class ModelClient(Protocol):
    """A model source with its own policy (fallbacks, credentials, timeouts) for the agent loop."""

    @property
    def spec(self) -> str:
        """The default ``provider:model`` spec."""
        ...

    max_tokens: int | None
    """Default output token limit reported to hooks, or None."""

    def open_stream(
        self, messages: list[republic.Message], *, spec: str, options: Mapping[str, Any]
    ) -> AbstractAsyncContextManager[tuple[str, republic.Stream[Any]]]:
        """Open a streaming completion for ``spec``; yield the provider name and the stream.

        ``spec`` is the default spec unless a ``LLMCallStarted`` handler redirected the call;
        ``options`` are the call's ``republic.ChatOptions``, tools included.
        """
        ...


Model = republic.ChatModel | ModelClient
"""What a run calls: a ``republic.ChatModel``, or a :class:`ModelClient`."""


class ChatModelClient:
    """Adapt a ``republic.ChatModel`` to :class:`ModelClient`; the caller owns the model's lifecycle.

    A call redirected to another model of the same provider reuses its client; one
    redirected to another provider uses a client configured from Republic's environment.
    """

    max_tokens: int | None = None

    def __init__(self, model: republic.ChatModel) -> None:
        self.model = model

    @property
    def spec(self) -> str:
        return f"{self.model.provider.name}:{self.model.name}"

    @asynccontextmanager
    async def open_stream(
        self, messages: list[republic.Message], *, spec: str, options: Mapping[str, Any]
    ) -> AsyncIterator[tuple[str, republic.Stream[Any]]]:
        provider, _, name = spec.partition(":")
        if spec == self.spec:
            async with self.model.stream(messages, **options) as stream:
                yield provider, stream
        elif provider == self.model.provider.name:
            async with self.model.provider.get_model(name).stream(messages, **options) as stream:
                yield provider, stream
        else:
            async with republic.get_model(spec) as model, model.stream(messages, **options) as stream:
                yield provider, stream


def model_client(model: Model) -> ModelClient:
    """Return ``model`` as a :class:`ModelClient`, adapting a ``republic.ChatModel``."""
    return ChatModelClient(model) if isinstance(model, republic.ChatModel) else model
