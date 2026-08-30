"""Lossless CloudEvent codecs used by concrete tape stores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from cloudevents.core.exceptions import CloudEventValidationError
from cloudevents.core.formats.json import JSONFormat
from cloudevents.core.v1.event import CloudEvent


class TapeCodecError(ValueError):
    """Raised when a tape event cannot be encoded or decoded."""


class TapeCodec(Protocol):
    """Encode one CloudEvent frame independently from a store."""

    @property
    def file_suffix(self) -> str: ...

    def encode(self, event: CloudEvent) -> bytes: ...

    def decode(self, frame: bytes) -> CloudEvent: ...


@dataclass(frozen=True)
class CloudEventJsonTapeCodec:
    """Encode one structured CloudEvents JSON event directly."""

    file_suffix: str = ".jsonl"

    def encode(self, event: CloudEvent) -> bytes:
        return JSONFormat().write(event)

    def decode(self, frame: bytes) -> CloudEvent:
        try:
            event = JSONFormat().read(CloudEvent, frame.decode())
        except (CloudEventValidationError, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise TapeCodecError("invalid structured CloudEvent") from exc
        return cast("CloudEvent", event)
