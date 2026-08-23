"""Fork and merge tape writes through a builtin sidecar."""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any, cast

from loguru import logger

from bub.sidecars import sidecar_owns_tape, sidecar_tape_name
from bub.store import AsyncTapeStore, InMemoryTapeStore, TapeQuery
from bub.tape import Tape, TapeEntry

FORK_MERGE_SIDECAR_NAME = "forkmerge"


class _ForkTapeStore:
    """In-memory write overlay owned by the forkmerge sidecar."""

    def __init__(self, parent: AsyncTapeStore, tape: str, *, sidecars: Iterable[str] = ()) -> None:
        self._parent = parent
        self._store = InMemoryTapeStore()
        self._tape = tape
        self._sidecars = tuple(dict.fromkeys(sidecars))
        self._managed_tapes = {tape, *self._sidecars}
        self._reset_tapes: set[str] = set()

    async def list_tapes(self) -> list[str]:
        return await self._parent.list_tapes()

    async def reset(self, tape: str) -> None:
        if tape not in self._managed_tapes:
            await self._parent.reset(tape)
            return
        self._store.reset(tape)
        self._reset_tapes.add(tape)

    async def fetch_all(self, query: TapeQuery[AsyncTapeStore]) -> Iterable[TapeEntry]:
        if query.tape not in self._managed_tapes:
            return await self._parent.fetch_all(query)

        parent_entries: Iterable[TapeEntry] = []
        if query.tape not in self._reset_tapes:
            try:
                parent_entries = await self._parent.fetch_all(query)
            except Exception:
                parent_entries = []
        fork_entries: list[TapeEntry] = []
        for entry in self._store.read(query.tape) or []:
            if entry.kind == "anchor":  # noqa: SIM102
                if query._after_last or (query._after_anchor and entry.payload.get("name") == query._after_anchor):
                    fork_entries.clear()
                    parent_entries = []
                    continue
            if query._kinds and entry.kind not in query._kinds:
                continue
            fork_entries.append(entry)
        entries = itertools.chain(parent_entries, fork_entries)
        return itertools.islice(entries, query._limit) if query._limit is not None else entries

    @staticmethod
    def _redact_prompt(prompt: list[dict]) -> Any:
        if not isinstance(prompt, list):
            return prompt
        return [part for part in prompt if part.get("type") == "text"]

    @staticmethod
    def _redact_payload(payload: dict) -> None:
        if "content" in payload:
            payload["content"] = _ForkTapeStore._redact_prompt(payload["content"])
        elif "prompt" in payload:
            payload["prompt"] = _ForkTapeStore._redact_prompt(payload["prompt"])

    async def append(self, tape: str, entry: TapeEntry) -> None:
        self._redact_payload(entry.payload)
        if tape not in self._managed_tapes:
            await self._parent.append(tape, entry)
            return
        self._store.append(tape, entry)

    async def merge_back(self) -> None:
        total = 0
        for sidecar in self._sidecars:
            entries = self._store.read(sidecar) or []
            try:
                if sidecar in self._reset_tapes:
                    await self._parent.reset(sidecar)
                for entry in entries:
                    await self._parent.append(sidecar, entry)
            except Exception as exc:
                logger.warning('Failed to merge sidecar "{}" into tape "{}": {}', sidecar, self._tape, exc)
                self._store.append(
                    self._tape,
                    TapeEntry.event(
                        "sidecar.merge",
                        {"tape": sidecar, "status": "error", "error": str(exc)},
                        context=False,
                    ),
                )
            else:
                total += len(entries)

        if self._tape in self._reset_tapes:
            await self._parent.reset(self._tape)
        entries = self._store.read(self._tape) or []
        for entry in entries:
            await self._parent.append(self._tape, entry)
        total += len(entries)
        if total:
            logger.info('Merged {} entries into tape fork "{}"', total, self._tape)


@dataclass
class TapeFork:
    """An isolated tape whose writes can be merged or discarded."""

    tape: Tape
    _store: _ForkTapeStore = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def merge(self) -> None:
        if self._closed:
            return
        await self._store.merge_back()
        self._closed = True

    async def discard(self) -> None:
        self._closed = True


@dataclass(frozen=True)
class ForkMergeSidecar:
    """Isolate tape writes and merge them only when requested."""

    name: str = field(default=FORK_MERGE_SIDECAR_NAME, init=False)
    owns_tape: bool = field(default=False, init=False)

    @classmethod
    def mounted(cls, tape: Tape) -> ForkMergeSidecar:
        sidecar = tape.get_sidecar(FORK_MERGE_SIDECAR_NAME)
        if sidecar is None or not callable(getattr(sidecar, "fork", None)):
            raise TypeError("forkmerge sidecar is not mounted")
        return cast("ForkMergeSidecar", sidecar)

    async def fork(self, tape: Tape) -> TapeFork:
        managed_sidecars = tuple(
            sidecar_tape_name(tape.name, sidecar.name) for sidecar in tape.sidecars if sidecar_owns_tape(sidecar)
        )
        store = _ForkTapeStore(tape.store, tape.name, sidecars=managed_sidecars)
        return TapeFork(tape=replace(tape, store=store), _store=store)
