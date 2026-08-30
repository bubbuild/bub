"""Explicit archive services for durable tape snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bub.store import TapeQuery, TapeStore
from bub.tape_codec import TapeCodec


class TapeArchiver(Protocol):
    """Persist a finite snapshot without making archive format part of Tape."""

    async def archive(self, tape: str, store: TapeStore, stamp: str) -> Path: ...


@dataclass(frozen=True)
class FileTapeArchiver:
    """Write a line-framed archive using an explicit record codec."""

    directory: Path
    codec: TapeCodec

    async def archive(self, tape: str, store: TapeStore, stamp: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        archive_path = self.directory / f"{tape}{self.codec.file_suffix}.{stamp}.bak"
        with archive_path.open("wb") as handle:
            async for entry in store.scan(TapeQuery(tape)):
                frame = self.codec.encode(entry.event)
                if b"\n" in frame:
                    raise ValueError("line tape codecs must encode exactly one newline-free frame")
                handle.write(frame + b"\n")
        return archive_path
