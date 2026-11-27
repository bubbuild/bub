from __future__ import annotations

import base64
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

type MessageKind = Literal["error", "normal", "command"]
type MediaType = Literal["image", "audio", "video", "document"]


def format_front_matter_message(metadata: Mapping[str, Any], body: str) -> str:
    """Render message content as YAML front matter followed by the body.

    Multi-line strings are emitted as ``|`` block scalars, so their lines stay inside the
    front matter by indentation and cannot be mistaken for the body or for other keys.

    >>> print(format_front_matter_message({"sender_id": "7", "reply_to": {"content": "a\\n\\nb"}}, "hi"))
    ---
    sender_id: '7'
    reply_to:
      content: |-
        a
    <BLANKLINE>
        b
    ---
    hi
    """
    import yaml

    class _Dumper(yaml.SafeDumper):
        pass

    def represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _Dumper.add_representer(str, represent_str)
    front_matter = yaml.dump(dict(metadata), Dumper=_Dumper, allow_unicode=True, sort_keys=False)
    return f"---\n{front_matter}---\n{body}"


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
    # What the sender wrote, for hooks that search or index messages, when ``content`` wraps it with channel
    # metadata; ``None`` means ``content`` is that text.
    text: str | None = None

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
        text = None
        if any(message.text is not None for message in batch):
            text = "\n".join(message.content if message.text is None else message.text for message in batch)
        media = [item for message in batch for item in message.media]
        return replace(template, content=content, media=media, text=text)
