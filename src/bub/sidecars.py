"""Contracts for tapes mounted beside a session tape."""

from __future__ import annotations

from typing import Protocol

SIDECAR_TAPE_MARKER = "__sidecar__"


class TapeSidecar(Protocol):
    """A named capability mounted beside a session tape."""

    @property
    def name(self) -> str: ...


def sidecar_owns_tape(sidecar: TapeSidecar) -> bool:
    """Return whether a sidecar owns a persistent sibling tape.

    Existing storage sidecars default to owning one. Capability-only sidecars,
    such as the builtin forkmerge plugin, opt out explicitly.
    """

    return getattr(sidecar, "owns_tape", True)


def sidecar_tape_name(owner: str, sidecar: str) -> str:
    """Return the physical tape name for a mounted sidecar."""

    return f"{owner}{SIDECAR_TAPE_MARKER}{sidecar}"
