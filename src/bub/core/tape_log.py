"""Physical tape protocol, records and offsets."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from bub.core.tape import TapeEntry
from bub.errors import BubError, ErrorKind


@dataclass(frozen=True, slots=True)
class TapeOffset:
    """Opaque, serializable boundary tied to a backend's log identity."""

    token: str


@dataclass(frozen=True, slots=True)
class TapeRecord:
    """A stored entry and the boundary immediately after that entry."""

    entry: TapeEntry
    offset: TapeOffset


class TapeStore(Protocol):
    """The mandatory async data plane for a canonical physical log."""

    async def append(self, tape: str, entry: TapeEntry) -> TapeRecord: ...

    async def tail(self, tape: str) -> TapeOffset: ...

    def stream(
        self, tape: str, *, start: TapeOffset | None = None, stop: TapeOffset | None = None
    ) -> AbstractAsyncContextManager[AsyncIterator[TapeRecord]]: ...


def encode_offset(identity: str, position: int) -> TapeOffset:
    return TapeOffset(json.dumps([identity, position], separators=(",", ":")))


def decode_offset(offset: TapeOffset, identity: str, tail: int) -> int:
    try:
        source, position = json.loads(offset.token)
        if source != identity or type(position) is not int or not 0 <= position <= tail:
            raise ValueError  # noqa: TRY301
    except (TypeError, ValueError) as exc:
        raise BubError(ErrorKind.INVALID_INPUT, "Invalid or expired tape offset.") from exc
    return position
