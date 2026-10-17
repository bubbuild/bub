"""Bub's builtin environment provider: the session's environment from turn state."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from bub.environment import ENVIRONMENT_STATE_KEY, Environment, LocalEnvironment

if TYPE_CHECKING:
    from bub.turn import TurnState


def environment_from_state(state: TurnState) -> Environment:
    """Return the session's environment, or a host environment for the state's workspace when none was provided.

    The fallback covers tools run outside a framework turn, for example by SDK callers or tests.
    """
    environment = state.get(ENVIRONMENT_STATE_KEY)
    if isinstance(environment, Environment):
        return environment
    workspace = state.get("_runtime_workspace")
    if workspace is not None and not isinstance(workspace, str | Path):
        raise TypeError("runtime workspace must be a filesystem path")
    return LocalEnvironment(workspace)
