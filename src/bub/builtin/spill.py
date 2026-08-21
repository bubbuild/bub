"""Chunked storage and bounded reads for oversized tool results."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

from loguru import logger
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub import config
from bub.configure import Settings
from bub.errors import BubError, ErrorKind
from bub.tape import Tape, TapeEntry, TapeQuery
from bub.tools import ToolContext, tool

SPILL_READ_TOOL_NAME = "spill.read"
SPILL_READ_MODEL_NAME = SPILL_READ_TOOL_NAME.replace(".", "_")
SPILL_CHUNK_BYTES = 16_384
MAX_READ_CHUNKS = 4
PREVIEW_CHARS = 600
SPILL_SIDECAR_NAME = "spill"


@config(name="spill")
class SpillSettings(Settings):
    """Configuration owned by the builtin spill sidecar."""

    model_config = SettingsConfigDict(env_prefix="BUB_SPILL_", extra="ignore", env_file=".env")

    threshold: int = Field(
        default=4096,
        ge=0,
        description="Estimated tokens (4 chars each) above which string tool results move to the spill sidecar.",
    )


def _chunk_anchor(handle: str, index: int) -> str:
    return f"spill/{handle}/chunk/{index}"


def _manifest_anchor(handle: str) -> str:
    return f"spill/{handle}/manifest"


def _utf8_chunks(encoded: bytes, chunk_bytes: int = SPILL_CHUNK_BYTES) -> Iterator[str]:
    start = 0
    while start < len(encoded):
        end = min(start + chunk_bytes, len(encoded))
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        yield encoded[start:end].decode("utf-8")
        start = end


def _preview(text: str) -> str:
    if len(text) <= PREVIEW_CHARS:
        return text
    head = PREVIEW_CHARS // 2
    tail = PREVIEW_CHARS - head
    omitted = len(text) - PREVIEW_CHARS
    return f"{text[:head]}\n...[{omitted:,} chars omitted]...\n{text[-tail:]}"


@dataclass(frozen=True)
class SpillManifest:
    handle: str
    chunks: int
    bytes: int
    chars: int
    lines: int

    @classmethod
    def from_entry(cls, entry: TapeEntry, handle: str) -> SpillManifest | None:
        if entry.kind != "event" or entry.payload.get("name") != "spill.manifest":
            return None
        data = entry.payload.get("data")
        if not isinstance(data, dict) or data.get("handle") != handle:
            return None
        values = (data.get("chunks"), data.get("bytes"), data.get("chars"), data.get("lines"))
        if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values):
            return None
        return cls(
            handle=handle,
            chunks=data["chunks"],
            bytes=data["bytes"],
            chars=data["chars"],
            lines=data["lines"],
        )


@dataclass(frozen=True)
class SpillPage:
    manifest: SpillManifest
    content: str
    start: int
    stop: int
    next_cursor: int
    complete: bool


class IncompleteSpillError(RuntimeError):
    """Raised when a manifest points to missing or invalid chunks."""


@dataclass(frozen=True)
class SpillStore:
    """Store spill chunks as ordinary entries in a sibling tape."""

    settings: SpillSettings
    name: str = field(default=SPILL_SIDECAR_NAME, init=False)

    async def _record_write(self, tape: Tape, data: dict[str, object], *, run_id: str) -> None:
        try:
            await tape.append_event("spill.write", data, run_id=run_id, context=False)
        except Exception as exc:
            logger.warning("spill write event failed run_id={} error={}", run_id, exc)

    async def spill_tool_result(self, tape: Tape, result: str, *, tool: str, run_id: str) -> str:
        threshold = self.settings.threshold
        if threshold <= 0 or tool in {SPILL_READ_TOOL_NAME, SPILL_READ_MODEL_NAME} or len(result) < threshold * 4:
            return result

        handle = uuid.uuid4().hex
        encoded = result.encode("utf-8")
        encoded_bytes = len(encoded)
        chunk_count = 0
        spill_tape = tape.sidecar_tape_name(self.name)
        try:
            for index, chunk in enumerate(_utf8_chunks(encoded)):
                await tape.store.append(spill_tape, TapeEntry.anchor(_chunk_anchor(handle, index)))
                await tape.store.append(
                    spill_tape,
                    TapeEntry.tool_result([chunk], spill_handle=handle, spill_chunk=index),
                )
                chunk_count = index + 1
            await tape.store.append(spill_tape, TapeEntry.anchor(_manifest_anchor(handle)))
            await tape.store.append(
                spill_tape,
                TapeEntry.event(
                    "spill.manifest",
                    {
                        "handle": handle,
                        "chunks": chunk_count,
                        "bytes": encoded_bytes,
                        "chars": len(result),
                        "lines": result.count("\n") + 1,
                        "tool": tool,
                    },
                    spill_handle=handle,
                    run_id=run_id,
                ),
            )
        except Exception as exc:
            logger.warning("tool result spill failed tool={} error={}", tool, exc)
            await self._record_write(
                tape,
                {
                    "status": "error",
                    "handle": handle,
                    "bytes": encoded_bytes,
                    "tool": tool,
                    "error": str(exc),
                },
                run_id=run_id,
            )
            return f"[tool output truncated: {encoded_bytes:,} bytes; spill storage failed]\n{_preview(result)}"

        await self._record_write(
            tape,
            {
                "status": "ok",
                "handle": handle,
                "bytes": encoded_bytes,
                "chunks": chunk_count,
                "tool": tool,
            },
            run_id=run_id,
        )

        return (
            f"[tool output spilled: {encoded_bytes:,} bytes in {chunk_count:,} chunks; handle: {handle}]\n"
            f"[read with: {SPILL_READ_MODEL_NAME}(handle={handle!r}, cursor=0, count=1, from_end=False)]\n"
            f"{_preview(result)}"
        )

    async def manifest(self, tape: Tape, handle: str) -> SpillManifest | None:
        query = (
            TapeQuery(tape=tape.sidecar_tape_name(self.name), store=tape.store)
            .after_anchor(_manifest_anchor(handle))
            .kinds("event")
            .limit(1)
        )
        try:
            entries = list(await tape.store.fetch_all(query))
        except BubError as exc:
            if exc.kind is ErrorKind.NOT_FOUND:
                return None
            raise
        if not entries:
            return None
        return SpillManifest.from_entry(entries[0], handle)

    async def read(
        self,
        tape: Tape,
        handle: str,
        *,
        cursor: int,
        count: int,
        from_end: bool,
    ) -> SpillPage | None:
        manifest = await self.manifest(tape, handle)
        if manifest is None:
            return None

        count = min(count, MAX_READ_CHUNKS)
        if from_end:
            stop = max(0, manifest.chunks - cursor)
            start = max(0, stop - count)
            next_cursor = cursor + (stop - start)
            complete = start == 0
        else:
            start = min(cursor, manifest.chunks)
            stop = min(start + count, manifest.chunks)
            next_cursor = stop
            complete = stop == manifest.chunks

        if start == stop:
            return SpillPage(manifest, "", start, stop, next_cursor, True)

        query = (
            TapeQuery(tape=tape.sidecar_tape_name(self.name), store=tape.store)
            .after_anchor(_chunk_anchor(handle, start))
            .kinds("tool_result")
            .limit(stop - start)
        )
        try:
            entries = list(await tape.store.fetch_all(query))
        except BubError as exc:
            if exc.kind is ErrorKind.NOT_FOUND:
                raise IncompleteSpillError(f"missing chunk {start} for handle {handle!r}") from exc
            raise
        if len(entries) != stop - start:
            raise IncompleteSpillError(f"missing chunks for handle {handle!r}")

        chunks: list[str] = []
        for index, entry in enumerate(entries, start=start):
            results = entry.payload.get("results")
            if (
                entry.meta.get("spill_handle") != handle
                or entry.meta.get("spill_chunk") != index
                or not isinstance(results, list)
                or len(results) != 1
                or not isinstance(results[0], str)
            ):
                raise IncompleteSpillError(f"invalid chunk {index} for handle {handle!r}")
            chunks.append(results[0])

        return SpillPage(manifest, "".join(chunks), start, stop, next_cursor, complete)


@tool(context=True, name=SPILL_READ_TOOL_NAME)
async def spill_read(
    handle: str,
    cursor: int = 0,
    count: int = 1,
    from_end: bool = False,
    *,
    context: ToolContext,
) -> str:
    """Read bounded chunks from an oversized tool result stored in the current session's spill tape."""
    if cursor < 0:
        return "`cursor` must be >= 0."
    if count < 1:
        return "`count` must be >= 1."

    spill = context.tape.get_sidecar(SPILL_SIDECAR_NAME)
    if not isinstance(spill, SpillStore):
        return "spill sidecar unavailable in this context."
    try:
        page = await spill.read(
            context.tape,
            handle,
            cursor=cursor,
            count=min(count, MAX_READ_CHUNKS),
            from_end=from_end,
        )
    except IncompleteSpillError as exc:
        return f"[incomplete spilled tool result: {exc}]"
    if page is None:
        return f"[no spilled tool result for handle {handle!r}]"

    shown = f"{page.start}-{page.stop - 1}" if page.stop > page.start else "none"
    return (
        f"[spilled tool result: {page.manifest.bytes:,} bytes, {page.manifest.chunks:,} chunks]\n"
        f"chunks: {shown}\n"
        f"next_cursor: {page.next_cursor}\n"
        f"complete: {str(page.complete).lower()}\n"
        f"content:\n{page.content}"
    )
