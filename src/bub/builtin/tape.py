import contextlib
import hashlib
import inspect
import json
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bub.builtin.store import ForkTapeStore
from bub.runtime import BubError
from bub.tape import AsyncTapeStore, Tape, TapeContext, TapeEntry, TapeQuery, build_messages


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
    def __init__(self, archive_path: Path, store: ForkTapeStore, context: TapeContext | None = None) -> None:
        self._archive_path = archive_path
        self._store = store
        self._context = context or TapeContext()

    def query(self, tape_name: str) -> TapeQuery[AsyncTapeStore]:
        return TapeQuery(tape=tape_name, store=self._store)

    async def info(self, tape_name: str) -> TapeInfo:
        entries = list(await self._store.fetch_all(self.query(tape_name)))
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
            name=tape_name,
            entries=len(entries),
            anchors=len(anchors),
            last_anchor=str(last_anchor) if last_anchor else None,
            entries_since_last_anchor=entries_since_last_anchor,
            last_token_usage=last_token_usage,
        )

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        anchors = list(await self._store.fetch_all(self.query(tape_name).kinds("anchor")))
        if not anchors:
            await self.handoff(tape_name, name="session/start", state={"owner": "human"})

    async def anchors(self, tape_name: str, limit: int = 20) -> list[AnchorSummary]:
        entries = list(await self._store.fetch_all(self.query(tape_name).kinds("anchor")))
        results: list[AnchorSummary] = []
        for entry in entries[-limit:]:
            name = str(entry.payload.get("name", "-"))
            state = entry.payload.get("state")
            state_dict: dict[str, object] = dict(state) if isinstance(state, dict) else {}
            results.append(AnchorSummary(name=name, state=state_dict))
        return results

    async def search(self, query: TapeQuery[AsyncTapeStore]) -> list[TapeEntry]:
        return list(await self._store.fetch_all(query))

    async def append_event(self, tape_name: str, name: str, payload: dict[str, Any], **meta: Any) -> None:
        await self._store.append(tape_name, TapeEntry.event(name, payload, **meta))

    async def read_messages(self, tape: Tape) -> list[dict[str, Any]]:
        query = tape.context.build_query(self.query(tape.name))
        entries = await self._store.fetch_all(query)
        messages = build_messages(entries, tape.context)
        if inspect.isawaitable(messages):
            messages = await messages
        return messages

    async def handoff(
        self,
        tape_name: str,
        *,
        name: str,
        state: dict[str, Any] | None = None,
        **meta: Any,
    ) -> list[TapeEntry]:
        entry = TapeEntry.anchor(name, state=state, **meta)
        event = TapeEntry.event("handoff", {"name": name, "state": state or {}}, **meta)
        await self._store.append(tape_name, entry)
        await self._store.append(tape_name, event)
        return [entry, event]

    async def record_chat(  # noqa: C901
        self,
        *,
        tape: str,
        run_id: str,
        system_prompt: str | None,
        new_messages: list[dict[str, Any]],
        response_text: str | None,
        context_error: BubError | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_results: list[Any] | None = None,
        error: BubError | None = None,
        response: Any | None = None,
        provider: str | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        meta = {"run_id": run_id}
        if system_prompt:
            await self._store.append(tape, TapeEntry.system(system_prompt, **meta))
        if context_error is not None:
            await self._store.append(tape, TapeEntry.error(context_error, **meta))
        for message in new_messages:
            await self._store.append(tape, TapeEntry.message(message, **meta))
        if tool_calls:
            await self._store.append(tape, TapeEntry.tool_call(tool_calls, **meta))
        if tool_results is not None:
            await self._store.append(tape, TapeEntry.tool_result(tool_results, **meta))
        if error is not None and error is not context_error:
            await self._store.append(tape, TapeEntry.error(error, **meta))
        if response_text is not None:
            await self._store.append(tape, TapeEntry.message({"role": "assistant", "content": response_text}, **meta))

        data: dict[str, Any] = {"status": "error" if error is not None else "ok"}
        resolved_usage = usage or self._extract_usage(response)
        if resolved_usage is not None:
            data["usage"] = resolved_usage
        if provider:
            data["provider"] = provider
        if model:
            data["model"] = model
        await self._store.append(tape, TapeEntry.event("run", data, **meta))

    @staticmethod
    def _extract_usage(response: object) -> dict[str, Any] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        if isinstance(usage, dict):
            return usage
        if isinstance(usage, BaseModel):
            payload = usage.model_dump(exclude_none=True)
            return payload if isinstance(payload, dict) else None
        return None

    async def _archive(self, tape_name: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._archive_path.mkdir(parents=True, exist_ok=True)
        archive_path = self._archive_path / f"{tape_name}.jsonl.{stamp}.bak"
        with archive_path.open("w", encoding="utf-8") as f:
            for entry in await self._store.fetch_all(self.query(tape_name)):
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return archive_path

    async def reset(self, tape_name: str, *, archive: bool = False) -> str:
        archive_path: Path | None = None
        if archive:
            archive_path = await self._archive(tape_name)
        await self._store.reset(tape_name)
        state = {"owner": "human"}
        if archive_path is not None:
            state["archived"] = str(archive_path)
        await self.handoff(tape_name, name="session/start", state=state)
        return f"Archived: {archive_path}" if archive_path else "ok"

    def session_tape(self, session_id: str, workspace: Path) -> Tape:
        workspace_hash = hashlib.md5(str(workspace.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        tape_name = (
            workspace_hash + "__" + hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )
        return Tape(name=tape_name, context=self._context)

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._store.fork(tape_name, merge_back=merge_back):
            yield
