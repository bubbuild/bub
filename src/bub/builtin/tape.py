from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from bub.builtin.store import ForkTapeStore
from bub.runtime import BubError
from bub.tape import (
    AsyncTapeStore,
    TapeContext,
    TapeEntry,
    TapeQuery,
    build_messages,
)


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


@dataclass(frozen=True)
class Tape:
    """Tape abstraction for recording agent interactions."""

    archive_path: Path
    store: AsyncTapeStore
    context: TapeContext
    _name: str | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        if self._name is None:
            raise ValueError("tape is not scoped")
        return self._name

    def with_context(self, context: TapeContext) -> Tape:
        return replace(self, context=context)

    def scoped(self, name: str, context: TapeContext | None = None) -> Tape:
        return replace(self, context=context or self.context, _name=name)

    def query(self) -> TapeQuery[AsyncTapeStore]:
        return TapeQuery(tape=self.name, store=self.store)

    async def info(self) -> TapeInfo:
        entries = list(await self.store.fetch_all(self.query()))
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
            name=self.name,
            entries=len(entries),
            anchors=len(anchors),
            last_anchor=str(last_anchor) if last_anchor else None,
            entries_since_last_anchor=entries_since_last_anchor,
            last_token_usage=last_token_usage,
        )

    async def ensure_bootstrap_anchor(self) -> None:
        anchors = list(await self.store.fetch_all(self.query().kinds("anchor")))
        if not anchors:
            await self.handoff(name="session/start", state={"owner": "human"})

    async def anchors(self, limit: int = 20) -> list[AnchorSummary]:
        entries = list(await self.store.fetch_all(self.query().kinds("anchor")))
        results: list[AnchorSummary] = []
        for entry in entries[-limit:]:
            name = str(entry.payload.get("name", "-"))
            state = entry.payload.get("state")
            state_dict: dict[str, object] = dict(state) if isinstance(state, dict) else {}
            results.append(AnchorSummary(name=name, state=state_dict))
        return results

    async def search(self, query: TapeQuery[AsyncTapeStore]) -> list[TapeEntry]:
        return list(await self.store.fetch_all(query))

    async def append_event(self, name: str, payload: dict[str, Any], **meta: Any) -> None:
        await self.store.append(self.name, TapeEntry.event(name, payload, **meta))

    async def read_messages(self) -> list[dict[str, Any]]:
        query = self.context.build_query(self.query())
        entries = await self.store.fetch_all(query)
        messages = build_messages(entries, self.context)
        if inspect.isawaitable(messages):
            messages = await messages
        return messages

    async def handoff(
        self,
        *,
        name: str,
        state: dict[str, Any] | None = None,
        **meta: Any,
    ) -> list[TapeEntry]:
        tape_name = self.name
        entry = TapeEntry.anchor(name, state=state, **meta)
        event = TapeEntry.event("handoff", {"name": name, "state": state or {}}, **meta)
        await self.store.append(tape_name, entry)
        await self.store.append(tape_name, event)
        return [entry, event]

    async def record_chat(  # noqa: C901
        self,
        *,
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
        tape_name = self.name
        meta = {"run_id": run_id}
        if system_prompt:
            await self.store.append(tape_name, TapeEntry.system(system_prompt, **meta))
        if context_error is not None:
            await self.store.append(tape_name, TapeEntry.error(context_error, **meta))
        for message in new_messages:
            await self.store.append(tape_name, TapeEntry.message(message, **meta))
        if tool_calls:
            await self.store.append(tape_name, TapeEntry.tool_call(tool_calls, **meta))
        if tool_results is not None:
            await self.store.append(tape_name, TapeEntry.tool_result(tool_results, **meta))
        if error is not None and error is not context_error:
            await self.store.append(tape_name, TapeEntry.error(error, **meta))
        if response_text is not None:
            await self.store.append(
                tape_name, TapeEntry.message({"role": "assistant", "content": response_text}, **meta)
            )

        data: dict[str, Any] = {"status": "error" if error is not None else "ok"}
        resolved_usage = usage or self._extract_usage(response)
        if resolved_usage is not None:
            data["usage"] = resolved_usage
        if provider:
            data["provider"] = provider
        if model:
            data["model"] = model
        await self.store.append(tape_name, TapeEntry.event("run", data, **meta))

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

    async def _archive(self) -> Path:
        tape_name = self.name
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.archive_path.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_path / f"{tape_name}.jsonl.{stamp}.bak"
        with archive_path.open("w", encoding="utf-8") as f:
            for entry in await self.store.fetch_all(self.query()):
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return archive_path

    async def reset(self, *, archive: bool = False) -> str:
        archive_path: Path | None = None
        if archive:
            archive_path = await self._archive()
        await self.store.reset(self.name)
        state = {"owner": "human"}
        if archive_path is not None:
            state["archived"] = str(archive_path)
        await self.handoff(name="session/start", state=state)
        return f"Archived: {archive_path}" if archive_path else "ok"

    def session_tape(self, session_id: str, workspace: Path, context: TapeContext | None = None) -> Tape:
        workspace_hash = hashlib.md5(str(workspace.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        tape_name = (
            workspace_hash + "__" + hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )
        return self.scoped(tape_name, context=context)

    @contextlib.asynccontextmanager
    async def fork_tape(self, merge_back: bool = True) -> AsyncGenerator[Tape, None]:
        fork_store = ForkTapeStore(self.store, self.name)
        forked = replace(self, store=fork_store)
        try:
            yield forked
        finally:
            if merge_back:
                await fork_store.merge_back()
