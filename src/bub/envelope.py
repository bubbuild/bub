"""Utilities for reading and normalizing user-defined envelopes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

type Envelope = Any


def field_of(message: Envelope, key: str, default: Any = None) -> Any:
    """Read a field from mapping-like or attribute-based messages."""

    if isinstance(message, Mapping):
        return message.get(key, default)
    return getattr(message, key, default)


def content_of(message: Envelope) -> str:
    """Get textual content from any envelope shape."""

    return str(field_of(message, "content", ""))


def normalize_envelope(message: Envelope) -> dict[str, Any]:
    """Convert arbitrary message objects to a mutable envelope mapping."""

    if isinstance(message, Mapping):
        return dict(message)
    if hasattr(message, "__dict__"):
        return dict(vars(message))
    return {"content": str(message)}


def unpack_batch(batch: Any) -> list[Envelope]:
    """Normalize one render_outbound return value to a list of envelopes."""

    if batch is None:
        return []
    if isinstance(batch, list | tuple):
        return list(batch)
    return [batch]
