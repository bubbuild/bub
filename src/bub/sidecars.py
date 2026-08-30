"""Specs for capabilities mounted beside Bub's authoritative tape."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bub.store import TapeStore
    from bub.tape import TapeRecord

SIDECAR_TAPE_MARKER = "__sidecar__"


@runtime_checkable
class TapeSidecar(Protocol):
    """Base spec implemented by every capability mounted through the sidecar hook."""

    @property
    def name(self) -> str: ...


@runtime_checkable
class SiblingTapeSidecar(TapeSidecar, Protocol):
    """Spec for a sidecar that owns one or more sibling tapes."""

    def sibling_tapes(self, owner: str) -> Iterable[str]: ...


@runtime_checkable
class TapeOverlaySidecar(TapeSidecar, Protocol):
    """Spec for a sidecar that creates an isolated TapeStore overlay."""

    def create_overlay(self, parent: TapeStore, owner: str, siblings: tuple[str, ...]) -> Any: ...


@runtime_checkable
class CommittedTapeSidecar(TapeSidecar, Protocol):
    """Spec for a sidecar that reacts to committed tape facts."""

    async def on_commit(self, tape: str, record: TapeRecord) -> None: ...


@dataclass(frozen=True)
class ForkOverlaySidecar:
    """Builtin overlay spec retaining writes until a fork merges."""

    name: str = field(default="fork-overlay", init=False)

    def create_overlay(self, parent: TapeStore, owner: str, siblings: tuple[str, ...]) -> Any:
        from bub.store import ForkTapeStore

        return ForkTapeStore(parent, owner, sidecars=siblings)


def sidecar_tape_name(owner: str, sidecar: str) -> str:
    """Return the conventional physical name for one sibling tape."""

    return f"{owner}{SIDECAR_TAPE_MARKER}{sidecar}"
