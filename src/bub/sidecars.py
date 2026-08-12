"""Contracts for tapes mounted beside a session tape."""

from __future__ import annotations

from typing import Protocol


class TapeSidecar(Protocol):
    """A named capability backed by a sibling tape."""

    @property
    def name(self) -> str: ...


def sidecar_tape_name(owner: str, sidecar: str) -> str:
    """Return the physical tape name for a mounted sidecar."""

    return f"{owner}__{sidecar}"
