"""Deprecated compatibility aliases for the former shared type module."""

import warnings

from bub.channels.contracts import MessageHandler
from bub.envelope import Envelope
from bub.turn import TurnState as State

__all__ = ["Envelope", "MessageHandler", "State"]

warnings.warn(
    "`bub.types` is deprecated; use `bub.envelope.Envelope`, `bub.turn.TurnState`, "
    "and `bub.channels.contracts.MessageHandler` instead.",
    DeprecationWarning,
    stacklevel=2,
)
