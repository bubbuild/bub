import importlib
import sys

import pytest

from bub.channels.contracts import MessageHandler
from bub.envelope import Envelope
from bub.turn import TurnState


def test_deprecated_types_reexports_compatibility_aliases() -> None:
    sys.modules.pop("bub.types", None)

    with pytest.warns(DeprecationWarning, match=r"`bub\.types` is deprecated"):
        legacy_types = importlib.import_module("bub.types")

    assert legacy_types.__all__ == ["Envelope", "MessageHandler", "State"]
    assert legacy_types.Envelope is Envelope
    assert legacy_types.MessageHandler is MessageHandler
    assert legacy_types.State is TurnState
