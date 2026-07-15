from typing import TYPE_CHECKING, Any

from .base import Channel, Interface, Lifecycle
from .message import ChannelMessage

if TYPE_CHECKING:
    from .manager import ChannelManager

__all__ = ["Channel", "ChannelManager", "ChannelMessage", "Interface", "Lifecycle"]


def __getattr__(name: str) -> Any:
    if name == "ChannelManager":
        from .manager import ChannelManager

        return ChannelManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
