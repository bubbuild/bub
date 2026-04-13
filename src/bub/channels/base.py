import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from typing import ClassVar

from republic import StreamEvent

from bub.channels.message import ChannelMessage


class Channel(ABC):
    """Base class for all channels"""

    name: ClassVar[str] = "base"

    @abstractmethod
    async def start(self, stop_event: asyncio.Event) -> None:
        """Start listening for events and dispatching to handlers."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""

    @property
    def needs_debounce(self) -> bool:
        """Whether this channel needs debounce to prevent overload. Default to False."""
        return False

    @property
    def enabled(self) -> bool:
        """Whether this channel is enabled. Default to True."""
        return True

    async def send(self, message: ChannelMessage) -> None:
        """Send a message to the channel. Optional to implement."""
        # Do nothing by default
        return

    def stream_events(self, message: ChannelMessage, stream: AsyncIterable[StreamEvent]) -> AsyncIterable[StreamEvent]:
        """Optionally wrap the output stream for this channel."""
        return stream
