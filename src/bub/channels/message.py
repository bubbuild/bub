from __future__ import annotations

import base64
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

type MessageKind = Literal["error", "normal", "command"]
type MediaType = Literal["image", "audio", "video", "document"]

_AUDIO_FORMAT_TO_MIME_TYPE = {
    "aiff": "audio/aiff",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "webm": "audio/webm",
}
_AUDIO_MIME_TYPE_TO_FORMAT = {
    **{mime_type: audio_format for audio_format, mime_type in _AUDIO_FORMAT_TO_MIME_TYPE.items()},
    "audio/x-aiff": "aiff",
    "audio/x-flac": "flac",
    "audio/x-m4a": "m4a",
    "audio/x-wav": "wav",
}


def audio_format_from_mime_type(mime_type: str) -> str:
    normalized = mime_type.partition(";")[0].strip().lower()
    return _AUDIO_MIME_TYPE_TO_FORMAT.get(normalized, normalized.removeprefix("audio/") or "unknown")


def audio_mime_type_from_format(audio_format: str) -> str:
    return _AUDIO_FORMAT_TO_MIME_TYPE.get(audio_format, f"audio/{audio_format}")


@dataclass
class MediaItem:
    """A media attachment on a channel message."""

    type: MediaType
    mime_type: str
    filename: str | None = None
    url: str | None = None
    data_fetcher: Callable[[], Awaitable[bytes | None]] | None = None

    async def get_url(self) -> str | None:
        """Get a URL for the media, fetching data if necessary."""
        if self.url:
            return self.url
        if self.data_fetcher is not None:
            data = await self.data_fetcher()
            if data is None:
                return None
            return f"data:{self.mime_type};base64,{base64.b64encode(data).decode('utf-8')}"
        return None


@dataclass
class ChannelMessage:
    """Structured message data from channels to framework."""

    session_id: str
    channel: str
    content: str
    chat_id: str = "default"
    is_active: bool = False
    kind: MessageKind = "normal"
    context: dict[str, Any] = field(default_factory=dict)
    media: list[MediaItem] = field(default_factory=list)
    lifespan: contextlib.AbstractAsyncContextManager | None = None
    output_channel: str = ""

    def __post_init__(self) -> None:
        self.context.update({"channel": "$" + self.channel, "chat_id": self.chat_id})
        if not self.output_channel:  # output to the same channel by default
            self.output_channel = self.channel

    @property
    def context_str(self) -> str:
        """String representation of the context for prompt building."""
        return "|".join(
            f"{key}={value}" for key, value in self.context.items() if not key.startswith("_")
        )  # ignore internal keys

    @classmethod
    def from_batch(cls, batch: list[ChannelMessage]) -> ChannelMessage:
        """Create a single message by combining a batch of messages."""
        if not batch:
            raise ValueError("Batch cannot be empty")
        template = batch[-1]
        content = "\n".join(message.content for message in batch)
        media = [item for message in batch for item in message.media]
        return replace(template, content=content, media=media)
