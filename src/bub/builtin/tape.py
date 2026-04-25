import contextlib
import hashlib
import json
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pydantic.dataclasses import dataclass
from republic import DEFAULT_TAPE_FORMAT, LLM, AsyncTapeStore, Tape, TapeEntry, TapeFormat, TapeQuery

from bub.builtin.store import ForkTapeStore


@dataclass(frozen=True)
class TapeInfo:
    """Runtime tape info summary."""

    name: str
    entries: int
    anchors: int
    last_anchor: str | None
    entries_since_last_anchor: int
    last_token_usage: int | None


@dataclass(frozen=True)
class AnchorSummary:
    """Rendered anchor summary."""

    name: str
    state: dict[str, object]


class TapeService:
    def __init__(
        self,
        llm: LLM,
        archive_path: Path,
        store: ForkTapeStore,
        tape_format: TapeFormat = DEFAULT_TAPE_FORMAT,
    ) -> None:
        self._llm = llm
        self._archive_path = archive_path
        self._store = store
        self._tape_format = tape_format

    async def info(self, tape_name: str) -> TapeInfo:
        tape = self._llm.tape(tape_name)
        entries = list(await tape.query_async.all())
        anchors = [(i, entry) for i, entry in enumerate(entries) if entry.kind == "anchor"]
        if anchors:
            last_anchor = anchors[-1][1].payload.get("name")
            entries_since_last_anchor = len(entries) - anchors[-1][0] - 1
        else:
            last_anchor = None
            entries_since_last_anchor = len(entries)
        last_token_usage: int | None = None
        for entry in reversed(entries):
            if entry.kind == "event" and entry.payload.get("name") == "run":
                with contextlib.suppress(AttributeError):
                    token_usage = entry.payload.get("data", {}).get("usage", {}).get("total_tokens")
                    if token_usage and isinstance(token_usage, int):
                        last_token_usage = token_usage
                        break
        return TapeInfo(
            name=tape.name,
            entries=len(entries),
            anchors=len(anchors),
            last_anchor=str(last_anchor) if last_anchor else None,
            entries_since_last_anchor=entries_since_last_anchor,
            last_token_usage=last_token_usage,
        )

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        tape = self._llm.tape(tape_name)
        anchors = list(await tape.query_async.kinds("anchor").all())
        if not anchors:
            await tape.handoff_async("session/start", state={"owner": "human"})

    async def anchors(self, tape_name: str, limit: int = 20) -> list[AnchorSummary]:
        tape = self._llm.tape(tape_name)
        entries = list(await tape.query_async.kinds("anchor").all())
        results: list[AnchorSummary] = []
        for entry in entries[-limit:]:
            name = str(entry.payload.get("name", "-"))
            state = entry.payload.get("state")
            state_dict: dict[str, object] = dict(state) if isinstance(state, dict) else {}
            results.append(AnchorSummary(name=name, state=state_dict))
        return results

    async def _archive(self, tape_name: str) -> Path:
        tape = self._llm.tape(tape_name)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._archive_path.mkdir(parents=True, exist_ok=True)
        archive_path = self._archive_path / f"{tape.name}.jsonl.{stamp}.bak"
        with archive_path.open("w", encoding="utf-8") as f:
            for entry in await tape.query_async.all():
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return archive_path

    async def reset(self, tape_name: str, *, archive: bool = False) -> str:
        tape = self._llm.tape(tape_name)
        archive_path: Path | None = None
        if archive:
            archive_path = await self._archive(tape_name)
        await tape.reset_async()
        state = {"owner": "human"}
        if archive_path is not None:
            state["archived"] = str(archive_path)
        await tape.handoff_async("session/start", state=state)
        return f"Archived: {archive_path}" if archive_path else "ok"

    async def handoff(self, tape_name: str, *, name: str, state: dict[str, Any] | None = None) -> list[TapeEntry]:
        tape = self._llm.tape(tape_name)
        entries = await tape.handoff_async(name, state=state)
        return cast(list[TapeEntry], entries)

    async def search(self, query: TapeQuery[AsyncTapeStore]) -> list[TapeEntry]:
        return list(await self._store.fetch_all(query))

    def query(self, tape_name: str) -> TapeQuery[AsyncTapeStore]:
        return self._llm.tape(tape_name).query_async

    async def append_event(self, tape_name: str, name: str, payload: dict[str, Any], **meta: Any) -> None:
        tape = self._llm.tape(tape_name)
        await tape.append_async(self._tape_format.event(name=name, data=payload, **meta))

    def session_tape(self, session_id: str, workspace: Path) -> Tape:
        workspace_hash = hashlib.md5(str(workspace.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        tape_name = (
            workspace_hash + "__" + hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )
        return self._llm.tape(tape_name)

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._store.fork(tape_name, merge_back=merge_back):
            yield
