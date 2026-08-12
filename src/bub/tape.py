"""Append-only tape primitives owned by Bub."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from bub.errors import BubError
from bub.sidecars import TapeSidecar, sidecar_tape_name

__all__ = [
    "LAST_ANCHOR",
    "AnchorSelector",
    "AnchorSummary",
    "AsyncTapeStore",
    "AsyncTapeStoreAdapter",
    "ContextSelector",
    "InMemoryQueryMixin",
    "InMemoryTapeStore",
    "SelectedMessages",
    "Tape",
    "TapeContext",
    "TapeEntry",
    "TapeInfo",
    "TapeQuery",
    "TapeStore",
    "UnavailableTapeStore",
    "build_messages",
    "is_async_tape_store",
    "utc_now",
]

if TYPE_CHECKING:
    from bub.store import (
        AsyncTapeStore,
        AsyncTapeStoreAdapter,
        InMemoryQueryMixin,
        InMemoryTapeStore,
        TapeQuery,
        TapeStore,
        UnavailableTapeStore,
        is_async_tape_store,
    )


_STORE_EXPORTS = frozenset({
    "AsyncTapeStore",
    "AsyncTapeStoreAdapter",
    "InMemoryQueryMixin",
    "InMemoryTapeStore",
    "TapeQuery",
    "TapeStore",
    "UnavailableTapeStore",
    "is_async_tape_store",
})


def __getattr__(name: str) -> Any:
    if name not in _STORE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from bub import store

    return getattr(store, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_STORE_EXPORTS})


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TapeEntry:
    """A single append-only entry in a tape."""

    id: int
    kind: str
    payload: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    date: str = field(default_factory=utc_now)

    def copy(self) -> TapeEntry:
        return TapeEntry(self.id, self.kind, dict(self.payload), dict(self.meta), self.date)

    @classmethod
    def message(cls, message: dict[str, Any], **meta: Any) -> TapeEntry:
        return cls(id=0, kind="message", payload=dict(message), meta=dict(meta))

    @classmethod
    def system(cls, content: str, **meta: Any) -> TapeEntry:
        return cls(id=0, kind="system", payload={"content": content}, meta=dict(meta))

    @classmethod
    def anchor(cls, name: str, state: dict[str, Any] | None = None, **meta: Any) -> TapeEntry:
        payload: dict[str, Any] = {"name": name}
        if state is not None:
            payload["state"] = dict(state)
        return cls(id=0, kind="anchor", payload=payload, meta=dict(meta))

    @classmethod
    def tool_call(cls, calls: list[dict[str, Any]], **meta: Any) -> TapeEntry:
        return cls(id=0, kind="tool_call", payload={"calls": calls}, meta=dict(meta))

    @classmethod
    def tool_result(cls, results: list[Any], **meta: Any) -> TapeEntry:
        return cls(id=0, kind="tool_result", payload={"results": results}, meta=dict(meta))

    @classmethod
    def error(cls, error: BubError, **meta: Any) -> TapeEntry:
        return cls(id=0, kind="error", payload=error.as_dict(), meta=dict(meta))

    @classmethod
    def event(cls, name: str, data: dict[str, Any] | None = None, **meta: Any) -> TapeEntry:
        payload: dict[str, Any] = {"name": name}
        if data is not None:
            payload["data"] = dict(data)
        return cls(id=0, kind="event", payload=payload, meta=dict(meta))


class _LastAnchor:
    def __repr__(self) -> str:
        return "LAST_ANCHOR"


LAST_ANCHOR = _LastAnchor()
type AnchorSelector = str | None | _LastAnchor
type SelectedMessages = list[dict[str, Any]] | Coroutine[Any, Any, list[dict[str, Any]]]
type ContextSelector = Callable[[Iterable[TapeEntry], "TapeContext"], SelectedMessages]


@dataclass(frozen=True)
class TapeContext:
    """Rules for selecting tape entries into a prompt context."""

    anchor: AnchorSelector = LAST_ANCHOR
    select: ContextSelector | None = None
    state: dict[str, Any] = field(default_factory=dict)

    def build_query(self, query: TapeQuery) -> TapeQuery:
        if self.anchor is None:
            return query
        if isinstance(self.anchor, _LastAnchor):
            return query.last_anchor()
        return query.after_anchor(self.anchor)


def build_messages(entries: Iterable[TapeEntry], context: TapeContext) -> SelectedMessages:
    if context.select is not None:
        return context.select(entries, context)
    return _default_messages(entries)


def _default_messages(entries: Iterable[TapeEntry]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for entry in entries:
        if entry.kind != "message":
            continue
        payload = entry.payload
        if isinstance(payload, dict):
            messages.append(dict(payload))
    return messages


@dataclass(frozen=True)
class TapeInfo:
    """Runtime tape info summary."""

    name: str
    entries: int
    anchors: int
    last_anchor: str | None
    entries_since_last_anchor: int
    last_token_usage: int | None
    last_token_cache_hit_rate: float | None


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
    sidecars: tuple[TapeSidecar, ...] = field(default=(), repr=False)
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
        from bub.store import TapeQuery

        return TapeQuery(tape=self.name, store=self.store)

    def get_sidecar(self, name: str) -> TapeSidecar | None:
        """Return a mounted sidecar by its public name."""

        return next((sidecar for sidecar in self.sidecars if sidecar.name == name), None)

    def sidecar_tape_name(self, name: str) -> str:
        """Return the sibling tape name for a mounted sidecar."""

        if self.get_sidecar(name) is None:
            raise KeyError(f"tape sidecar {name!r} is not mounted")
        return sidecar_tape_name(self.name, name)

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
        last_token_cache_hit_rate: float | None = None
        for entry in reversed(entries):
            if entry.kind == "event" and entry.payload.get("name") == "run":
                data = entry.payload.get("data")
                usage = data.get("usage") if isinstance(data, Mapping) else None
                if not isinstance(usage, Mapping):
                    continue
                token_usage = usage.get("total_tokens")
                if not isinstance(token_usage, int) or isinstance(token_usage, bool):
                    continue
                last_token_usage = token_usage
                prompt_tokens = usage.get("prompt_tokens")
                prompt_details = usage.get("prompt_tokens_details")
                cached_tokens = prompt_details.get("cached_tokens") if isinstance(prompt_details, Mapping) else None
                if (
                    isinstance(prompt_tokens, int)
                    and not isinstance(prompt_tokens, bool)
                    and prompt_tokens > 0
                    and isinstance(cached_tokens, int)
                    and not isinstance(cached_tokens, bool)
                ):
                    last_token_cache_hit_rate = cached_tokens / prompt_tokens
                break
        return TapeInfo(
            name=self.name,
            entries=len(entries),
            anchors=len(anchors),
            last_anchor=str(last_anchor) if last_anchor else None,
            entries_since_last_anchor=entries_since_last_anchor,
            last_token_usage=last_token_usage,
            last_token_cache_hit_rate=last_token_cache_hit_rate,
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
        context_entries = (entry for entry in entries if entry.meta.get("context") is not False)
        messages = build_messages(context_entries, self.context)
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

    async def _archive_tape(self, tape_name: str, stamp: str) -> Path:
        from bub.store import TapeQuery

        self.archive_path.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_path / f"{tape_name}.jsonl.{stamp}.bak"
        with archive_path.open("w", encoding="utf-8") as f:
            query = TapeQuery(tape=tape_name, store=self.store)
            for entry in await self.store.fetch_all(query):
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        return archive_path

    @staticmethod
    def _sidecar_lifecycle_data(
        *,
        sidecar: str,
        status: str,
        reason: str,
        archive_path: Path | None = None,
        error: Exception | None = None,
        cause: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"sidecar": sidecar, "status": status, "reason": reason}
        if archive_path is not None:
            data["archive"] = str(archive_path)
        if error is not None:
            data["error"] = str(error)
        if cause is not None:
            data["cause"] = cause
        return data

    async def _try_archive_sidecar(
        self,
        sidecar: TapeSidecar,
        *,
        reason: str,
        stamp: str | None = None,
    ) -> tuple[Path | None, dict[str, Any]]:
        archive_stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            archive_path = await self._archive_tape(sidecar_tape_name(self.name, sidecar.name), archive_stamp)
        except Exception as exc:
            return None, self._sidecar_lifecycle_data(
                sidecar=sidecar.name,
                status="error",
                reason=reason,
                error=exc,
            )
        return archive_path, self._sidecar_lifecycle_data(
            sidecar=sidecar.name,
            status="ok",
            reason=reason,
            archive_path=archive_path,
        )

    async def _try_reset_sidecar(self, sidecar: TapeSidecar, *, reason: str) -> dict[str, Any]:
        try:
            await self.store.reset(sidecar_tape_name(self.name, sidecar.name))
        except Exception as exc:
            return self._sidecar_lifecycle_data(sidecar=sidecar.name, status="error", reason=reason, error=exc)
        return self._sidecar_lifecycle_data(sidecar=sidecar.name, status="ok", reason=reason)

    def _require_sidecar(self, name: str) -> TapeSidecar:
        sidecar = self.get_sidecar(name)
        if sidecar is None:
            raise KeyError(f"tape sidecar {name!r} is not mounted")
        return sidecar

    async def archive_sidecar(self, name: str, *, reason: str = "manual") -> str:
        """Archive one mounted sidecar without changing the main tape."""

        sidecar = self._require_sidecar(name)
        archive_path, event_data = await self._try_archive_sidecar(sidecar, reason=reason)
        await self.append_event("sidecar.archive", event_data, context=False)
        return (
            f"Archived {name}: {archive_path}"
            if archive_path is not None
            else f"{name} archive failed: {event_data['error']}"
        )

    async def reset_sidecar(self, name: str, *, archive: bool = False, reason: str = "gc") -> str:
        """Reset one mounted sidecar and record the outcome on the main tape."""

        sidecar = self._require_sidecar(name)
        archive_path: Path | None = None
        archive_data: dict[str, Any] | None = None
        if archive:
            archive_path, archive_data = await self._try_archive_sidecar(sidecar, reason=reason)

        if archive_data is not None and archive_data["status"] == "error":
            reset_data = self._sidecar_lifecycle_data(
                sidecar=name,
                status="skipped",
                reason=reason,
                cause="archive_failed",
            )
        else:
            reset_data = await self._try_reset_sidecar(sidecar, reason=reason)
        if archive_data is not None:
            await self.append_event("sidecar.archive", archive_data, context=False)
        await self.append_event("sidecar.reset", reset_data, context=False)

        if reset_data["status"] == "error":
            return f"{name} reset failed: {reset_data['error']}"
        if reset_data["status"] == "skipped" and archive_data is not None:
            return f"{name} archive failed: {archive_data['error']}; {name} reset skipped"
        return f"Archived {name}: {archive_path}" if archive_path is not None else "ok"

    async def reset(self, *, archive: bool = False) -> str:
        archive_path: Path | None = None
        sidecar_archives: dict[str, dict[str, Any]] = {}
        if archive:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            archive_path = await self._archive_tape(self.name, stamp)
            for sidecar in self.sidecars:
                _, sidecar_archive = await self._try_archive_sidecar(sidecar, reason="tape.reset", stamp=stamp)
                sidecar_archives[sidecar.name] = sidecar_archive
        await self.store.reset(self.name)
        state = {"owner": "human"}
        if archive_path is not None:
            state["archived"] = str(archive_path)
        await self.handoff(name="session/start", state=state)
        for sidecar in self.sidecars:
            archive_data = sidecar_archives.get(sidecar.name)
            if archive_data is not None and archive_data["status"] == "error":
                reset_data = self._sidecar_lifecycle_data(
                    sidecar=sidecar.name,
                    status="skipped",
                    reason="tape.reset",
                    cause="archive_failed",
                )
            else:
                reset_data = await self._try_reset_sidecar(sidecar, reason="tape.reset")
            if archive_data is not None:
                await self.append_event("sidecar.archive", archive_data, context=False)
            await self.append_event("sidecar.reset", reset_data, context=False)
        return f"Archived: {archive_path}" if archive_path else "ok"

    def session_tape(self, session_id: str, workspace: Path, context: TapeContext | None = None) -> Tape:
        workspace_hash = hashlib.md5(str(workspace.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        tape_name = (
            workspace_hash + "__" + hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )
        return self.scoped(tape_name, context=context)

    @contextlib.asynccontextmanager
    async def fork_tape(self, merge_back: bool = True) -> AsyncGenerator[Tape, None]:
        from bub.store import ForkTapeStore

        managed_sidecars = tuple(sidecar_tape_name(self.name, sidecar.name) for sidecar in self.sidecars)
        fork_store = ForkTapeStore(self.store, self.name, sidecars=managed_sidecars)
        forked = replace(self, store=fork_store)
        try:
            yield forked
        finally:
            if merge_back:
                await fork_store.merge_back()
