"""Append-only tape primitives owned by Bub."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import inspect
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from cloudevents.core.v1.event import CloudEvent
from loguru import logger

from bub.errors import BubError
from bub.sidecars import CommittedTapeSidecar, SiblingTapeSidecar, TapeOverlaySidecar, TapeSidecar

__all__ = [
    "BUB_EVENT_PREFIX",
    "LAST_ANCHOR",
    "AnchorSelector",
    "AnchorSummary",
    "ContextSelector",
    "SelectedMessages",
    "Tape",
    "TapeContext",
    "TapeInfo",
    "TapeRecord",
    "anchor_event",
    "bub_event",
    "bub_event_type",
    "build_messages",
    "correlation_extensions",
    "error_event",
    "event_data",
    "event_extension",
    "event_payload",
    "event_time",
    "message_event",
]

if TYPE_CHECKING:
    from bub.store import TapeQuery, TapeStore
    from bub.tape_archive import TapeArchiver


BUB_EVENT_PREFIX = "build.bub"
_BUB_SOURCE = "https://bub.build"
_EVENT_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_EXTENSION_NAME = re.compile(r"^[a-z0-9]+$")
_ATIF_SOURCES = frozenset({"system", "user", "agent"})


def bub_event_type(name: str, *, version: int = 1) -> str:
    """Return the versioned CloudEvents-compatible type for one Bub occurrence."""

    if not _EVENT_NAME.fullmatch(name):
        raise ValueError(f"invalid Bub event name: {name!r}")
    if version < 1:
        raise ValueError("Bub event version must be positive")
    return f"{BUB_EVENT_PREFIX}.{name}.v{version}"


def correlation_extensions(state: Mapping[str, Any], **extensions: Any) -> dict[str, Any]:
    """Collect Bub correlation identifiers and non-null CloudEvents extensions."""

    correlated = {
        key: state.get(key) for key in ("session_id", "turn_id", "invocation_id", "model_call_id", "tool_call_id")
    }
    correlated.update(extensions)
    return {key: value for key, value in correlated.items() if value is not None}


def _extension_name(name: str) -> str:
    normalized = f"bub{name.replace('_', '').lower()}"
    if not _EXTENSION_NAME.fullmatch(normalized):
        raise ValueError(f"invalid Bub CloudEvents extension name: {name!r}")
    return normalized


def _event_attributes(extensions: Mapping[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "source": _BUB_SOURCE,
        "datacontenttype": "application/json",
    }
    for name, value in extensions.items():
        if value is None:
            continue
        if not isinstance(value, bool | int | str | bytes | datetime):
            raise TypeError(f"Bub CloudEvents extension {name!r} must be a CloudEvents scalar")
        attributes[_extension_name(name)] = value
    return attributes


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _atif_content(value: Any) -> str | list[dict[str, Any]]:
    """Normalize provider message content to ATIF's text/image vocabulary."""

    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return _json_text(value)

    parts: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, Mapping):
            parts.append({"type": "text", "text": _json_text(part)})
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append({"type": "text", "text": part["text"]})
            continue
        image_url = part.get("image_url")
        image_path = image_url.get("url") if isinstance(image_url, Mapping) else None
        if part.get("type") == "image_url" and isinstance(image_path, str):
            source: dict[str, Any] = {"path": image_path}
            if image_path.startswith("data:image/") and ";" in image_path:
                source["media_type"] = image_path[5 : image_path.index(";")]
            parts.append({"type": "image", "source": source})
            continue
        parts.append({"type": "text", "text": _json_text(part)})
    return parts


def _atif_step(
    source: Literal["system", "user", "agent"],
    message: Any = "",
    *,
    timestamp: datetime,
    **fields: Any,
) -> dict[str, Any]:
    """Build an ATIF StepObject used directly as CloudEvent data.

    ``step_id`` is provisional until a store commits the event. A committed
    ``TapeRecord`` always rewrites it to the record cursor.
    """

    if source not in _ATIF_SOURCES:
        raise ValueError(f"invalid ATIF source: {source!r}")
    step: dict[str, Any] = {
        "step_id": 1,
        "source": source,
        "message": _atif_content(message),
        "timestamp": timestamp.isoformat(),
    }
    step.update({name: copy.deepcopy(value) for name, value in fields.items() if value is not None})
    return step


def bub_event(
    name: str,
    data: dict[str, Any] | None = None,
    *,
    id_: str | None = None,
    time: datetime | None = None,
    **extensions: Any,
) -> CloudEvent:
    """Create a Bub fact as a CloudEvent carrying an ATIF system step."""

    extensions.pop("context", None)
    return _system_event(name, data, id_=id_, time=time, context=False, **extensions)


def _system_event(
    name: str,
    data: dict[str, Any] | None,
    *,
    id_: str | None = None,
    time: datetime | None = None,
    context: bool,
    **extensions: Any,
) -> CloudEvent:
    extensions["context"] = context
    event_time = time or datetime.now(UTC)
    step = _atif_step(
        "system",
        "",
        timestamp=event_time,
        observation={"results": [{"content": _json_text(data or {})}]},
    )
    return _cloud_event(name, step, id_=id_, time=event_time, **extensions)


def message_event(
    message: dict[str, Any],
    *,
    llm_call_count: int | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[Any] | None = None,
    model: str | None = None,
    usage: dict[str, Any] | None = None,
    id_: str | None = None,
    time: datetime | None = None,
    **extensions: Any,
) -> CloudEvent:
    role = message.get("role")
    if role not in {"system", "user", "assistant"}:
        raise ValueError(f"invalid message role: {role!r}")
    source: Literal["system", "user", "agent"]
    source = "agent" if role == "assistant" else "system" if role == "system" else "user"
    extensions.setdefault("context", True)
    event_time = time or datetime.now(UTC)
    has_agent_fields = (
        llm_call_count is not None
        or bool(tool_calls)
        or tool_results is not None
        or model is not None
        or usage is not None
    )
    if source != "agent" and has_agent_fields:
        raise ValueError("ATIF model, tool, and metrics fields require an agent message")
    fields = _agent_step_fields(llm_call_count, tool_calls, tool_results, model, usage) if source == "agent" else {}
    message_extra = {key: value for key, value in message.items() if key not in {"role", "content"}}
    if message_extra:
        fields["extra"] = {"bub": {"message": message_extra}}
    step = _atif_step(source, message.get("content", ""), timestamp=event_time, **fields)
    return _cloud_event("message", step, id_=id_, time=event_time, **extensions)


def _agent_step_fields(
    llm_call_count: int | None,
    tool_calls: list[dict[str, Any]] | None,
    tool_results: list[Any] | None,
    model: str | None,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    if llm_call_count is not None and (
        not isinstance(llm_call_count, int) or isinstance(llm_call_count, bool) or llm_call_count < 0
    ):
        raise ValueError("ATIF llm_call_count must be a non-negative integer")
    if llm_call_count == 0 and usage is not None:
        raise ValueError("ATIF metrics require at least one LLM call")
    fields: dict[str, Any] = {}
    if llm_call_count is not None:
        fields["llm_call_count"] = llm_call_count
    normalized_calls, call_ids = _atif_tool_calls(tool_calls or [])
    if normalized_calls:
        fields["tool_calls"] = normalized_calls
    if tool_results is not None:
        observations: list[dict[str, Any]] = []
        for index, result in enumerate(tool_results):
            observation: dict[str, Any] = {"content": _json_text(result)}
            if index < len(call_ids):
                observation["source_call_id"] = call_ids[index]
            observations.append(observation)
        fields["observation"] = {"results": observations}
    if model is not None:
        fields["model_name"] = model
    if usage is not None:
        fields["metrics"] = _atif_metrics(usage)
    return fields


def anchor_event(name: str, state: dict[str, Any] | None = None, **extensions: Any) -> CloudEvent:
    extensions.pop("context", None)
    return _system_event("context.anchor", {"name": name, "state": state or {}}, context=True, **extensions)


def _atif_tool_calls(calls: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    normalized: list[dict[str, Any]] = []
    call_ids: list[str] = []
    for index, call in enumerate(calls):
        function = call.get("function")
        function = function if isinstance(function, dict) else {}
        call_id = str(call.get("id") or f"call-{index + 1}")
        arguments = function.get("arguments", call.get("arguments", {}))
        extra: dict[str, Any] | None = None
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                extra = {"bub_raw_arguments": arguments}
                arguments = {}
        if not isinstance(arguments, dict):
            extra = {"bub_raw_arguments": arguments}
            arguments = {}
        item: dict[str, Any] = {
            "tool_call_id": call_id,
            "function_name": str(function.get("name") or call.get("name") or "unknown"),
            "arguments": arguments,
        }
        if extra:
            item["extra"] = extra
        normalized.append(item)
        call_ids.append(call_id)
    return normalized, call_ids


def _atif_metrics(usage: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            metrics[name] = value
    cost = usage.get("cost", usage.get("cost_usd"))
    if isinstance(cost, int | float) and not isinstance(cost, bool):
        metrics["cost"] = cost
    details = usage.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        cached = details.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            metrics["cached_tokens"] = cached
    return metrics


def error_event(error: BubError, **extensions: Any) -> CloudEvent:
    return bub_event("error", error.as_dict(), **extensions)


def _cloud_event(
    name: str,
    step: dict[str, Any],
    *,
    id_: str | None = None,
    time: datetime,
    **extensions: Any,
) -> CloudEvent:
    attributes = _event_attributes(extensions)
    attributes.update({"type": bub_event_type(name), "time": time})
    if id_ is not None:
        attributes["id"] = id_
    return CloudEvent(attributes, step)


def event_data(event: CloudEvent) -> dict[str, Any]:
    data = event.get_data()
    if not isinstance(data, dict):
        raise TypeError("Bub CloudEvent data must be an object")
    return data


def event_payload(event: CloudEvent) -> dict[str, Any]:
    """Return Bub's structured payload from an ATIF-native event, if present."""

    data = event_data(event)
    observation = data.get("observation")
    if not isinstance(observation, Mapping):
        return {}
    results = observation.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        return {}
    content = results[0].get("content")
    if not isinstance(content, str):
        return {}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def event_extension(event: CloudEvent, name: str) -> Any:
    return event.get_extension(_extension_name(name))


def event_time(event: CloudEvent) -> datetime:
    timestamp = event.get_time()
    if timestamp is None:
        raise ValueError("Bub CloudEvent is missing the time attribute")
    return timestamp


@dataclass(frozen=True, eq=False)
class TapeRecord:
    """A committed CloudEvent and its position in one tape."""

    cursor: int
    event: CloudEvent

    def __post_init__(self) -> None:
        if self.cursor < 1:
            raise ValueError("Tape cursor must be positive")
        data = copy.deepcopy(event_data(self.event))
        source = data.get("source")
        if source not in _ATIF_SOURCES or "message" not in data:
            raise ValueError("CloudEvent data must be an ATIF StepObject")
        if source == "user" and "observation" in data:
            raise ValueError("ATIF user steps cannot contain observations")
        if source != "agent" and any(
            field in data for field in ("tool_calls", "model_name", "metrics", "reasoning_effort", "reasoning_content")
        ):
            raise ValueError("ATIF model and tool fields require an agent step")
        data["step_id"] = self.cursor
        attributes = copy.deepcopy(self.event.get_attributes())
        object.__setattr__(self, "event", CloudEvent(attributes, data))

    def copy(self, *, cursor: int | None = None) -> TapeRecord:
        return TapeRecord(cursor=self.cursor if cursor is None else cursor, event=self.event)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, TapeRecord)
            and self.cursor == other.cursor
            and self.event.get_attributes() == other.event.get_attributes()
            and self.event.get_data() == other.event.get_data()
        )


class _LastAnchor:
    def __repr__(self) -> str:
        return "LAST_ANCHOR"


LAST_ANCHOR = _LastAnchor()
type AnchorSelector = str | None | _LastAnchor
type SelectedMessages = list[dict[str, Any]] | Coroutine[Any, Any, list[dict[str, Any]]]
type ContextSelector = Callable[[Iterable[TapeRecord], "TapeContext"], SelectedMessages]


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


def build_messages(entries: Iterable[TapeRecord], context: TapeContext) -> SelectedMessages:
    if context.select is None:
        from bub.builtin.context import select_messages

        return select_messages(entries, context)
    return context.select(entries, context)


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


class _TapeFeed:
    """Best-effort wakeups for consumers that resume from durable cursors."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[int]]] = {}
        self._generations: dict[str, int] = {}

    def subscribe(self, tape: str) -> asyncio.Queue[int]:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        self._subscribers.setdefault(tape, set()).add(queue)
        return queue

    def unsubscribe(self, tape: str, queue: asyncio.Queue[int]) -> None:
        subscribers = self._subscribers.get(tape)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(tape, None)

    def publish(self, tape: str) -> None:
        for queue in tuple(self._subscribers.get(tape, ())):
            if queue.empty():
                queue.put_nowait(self.generation(tape))

    def generation(self, tape: str) -> int:
        return self._generations.get(tape, 0)

    def reset(self, tape: str) -> None:
        self._generations[tape] = self.generation(tape) + 1
        self.publish(tape)


@dataclass(frozen=True)
class Tape:
    """Tape abstraction for recording agent interactions."""

    store: TapeStore
    context: TapeContext
    archiver: TapeArchiver | None = field(default=None, repr=False)
    sidecars: tuple[TapeSidecar, ...] = field(default=(), repr=False)
    _name: str | None = field(default=None, repr=False)
    _feed: _TapeFeed = field(default_factory=_TapeFeed, repr=False, compare=False)

    @property
    def name(self) -> str:
        if self._name is None:
            raise ValueError("tape is not scoped")
        return self._name

    def with_context(self, context: TapeContext) -> Tape:
        return replace(self, context=context)

    def scoped(self, name: str, context: TapeContext | None = None) -> Tape:
        return replace(self, context=context or self.context, _name=name)

    def query(self) -> TapeQuery:
        from bub.store import TapeQuery

        return TapeQuery(tape=self.name)

    def get_sidecar(self, name: str) -> TapeSidecar | None:
        """Return a mounted sidecar by its public name."""

        return next((sidecar for sidecar in self.sidecars if sidecar.name == name), None)

    def sidecar_tape_name(self, name: str) -> str:
        """Return the sibling tape name for a mounted sidecar."""

        sidecar = self.get_sidecar(name)
        if sidecar is None:
            raise KeyError(f"tape sidecar {name!r} is not mounted")
        if not isinstance(sidecar, SiblingTapeSidecar):
            raise KeyError(f"tape sidecar {name!r} does not provide a sibling tape")
        tapes = tuple(sidecar.sibling_tapes(self.name))
        if len(tapes) != 1:
            raise KeyError(f"tape sidecar {name!r} does not provide exactly one sibling tape")
        return tapes[0]

    def _sibling_sidecars(self) -> tuple[SiblingTapeSidecar, ...]:
        return tuple(sidecar for sidecar in self.sidecars if isinstance(sidecar, SiblingTapeSidecar))

    def _sibling_tapes(self) -> tuple[str, ...]:
        return tuple(tape for sidecar in self._sibling_sidecars() for tape in sidecar.sibling_tapes(self.name))

    async def info(self) -> TapeInfo:
        records = [record async for record in self.store.scan(self.query())]
        anchor_type = bub_event_type("context.anchor")
        anchors = [(i, record) for i, record in enumerate(records) if record.event.get_type() == anchor_type]
        if anchors:
            last_anchor = event_payload(anchors[-1][1].event).get("name")
            entries_since_last_anchor = len(records) - anchors[-1][0] - 1
        else:
            last_anchor = None
            entries_since_last_anchor = len(records)
        last_token_usage: int | None = None
        last_token_cache_hit_rate: float | None = None
        for record in reversed(records):
            usage = event_data(record.event).get("metrics")
            if not isinstance(usage, Mapping):
                continue
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if (
                not isinstance(prompt_tokens, int)
                or isinstance(prompt_tokens, bool)
                or not isinstance(completion_tokens, int)
                or isinstance(completion_tokens, bool)
            ):
                continue
            last_token_usage = prompt_tokens + completion_tokens
            cached_tokens = usage.get("cached_tokens")
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
            entries=len(records),
            anchors=len(anchors),
            last_anchor=str(last_anchor) if last_anchor else None,
            entries_since_last_anchor=entries_since_last_anchor,
            last_token_usage=last_token_usage,
            last_token_cache_hit_rate=last_token_cache_hit_rate,
        )

    async def ensure_bootstrap_anchor(self) -> None:
        anchors = self.store.scan(self.query().types(bub_event_type("context.anchor")).limit(1))
        if await anext(anchors, None) is None:
            await self.handoff(name="session/start", state={"owner": "human"})

    async def anchors(self, limit: int = 20) -> list[AnchorSummary]:
        query = self.query().types(bub_event_type("context.anchor"))
        records = [record async for record in self.store.scan(query)]
        results: list[AnchorSummary] = []
        for record in records[-limit:]:
            data = event_payload(record.event)
            name = str(data.get("name", "-"))
            state = data.get("state")
            state_dict: dict[str, object] = dict(state) if isinstance(state, dict) else {}
            results.append(AnchorSummary(name=name, state=state_dict))
        return results

    async def append(self, event: CloudEvent, *, tape: str | None = None) -> TapeRecord:
        """Persist one CloudEvent and wake followers only after the append commits."""

        tape_name = tape or self.name
        committed = await self.store.append(tape_name, event)
        self._feed.publish(tape_name)
        for sidecar in self.sidecars:
            if not isinstance(sidecar, CommittedTapeSidecar):
                continue
            try:
                await sidecar.on_commit(tape_name, committed)
            except Exception as exc:
                logger.warning(
                    "Committed tape sidecar failed sidecar={} tape={} event_id={} error={}",
                    sidecar.name,
                    tape_name,
                    committed.event.get_id(),
                    exc,
                )
        return committed

    async def append_event(self, name: str, data: dict[str, Any], **extensions: Any) -> TapeRecord:
        return await self.append(bub_event(name, data, **extensions))

    async def record_operation(
        self,
        operation: str,
        phase: Literal["started", "completed", "failed", "cancelled"],
        payload: dict[str, Any] | None = None,
        **extensions: Any,
    ) -> TapeRecord:
        """Persist one Bub runtime operation fact outside prompt context."""

        correlated = correlation_extensions(self.context.state, **extensions)
        return await self.append_event(f"{operation}.{phase}", payload or {}, **correlated)

    async def stream(self, query: TapeQuery | None = None, *, follow: bool = False) -> AsyncIterator[TapeRecord]:
        """Scan committed entries and optionally wait for later committed entries."""

        selected = query or self.query()
        if not follow:
            async for entry in self.store.scan(selected):
                yield entry
            return
        if selected._limit is not None:
            raise ValueError("a following tape stream cannot have a finite limit")

        wakeup = self._feed.subscribe(selected.tape)
        cursor = selected._after_cursor
        generation = self._feed.generation(selected.tape)
        base_query = selected
        try:
            while True:
                resumed = base_query if cursor is None else base_query.after(cursor)
                async for record in self.store.scan(resumed):
                    cursor = record.cursor
                    yield record
                notified_generation = await wakeup.get()
                if notified_generation != generation:
                    generation = notified_generation
                    cursor = None
                    base_query = replace(selected, _after_cursor=None)
        finally:
            self._feed.unsubscribe(selected.tape, wakeup)

    async def read_messages(self) -> list[dict[str, Any]]:
        query = self.context.build_query(self.query())
        records = [record async for record in self.store.scan(query)]
        context_records = (record for record in records if event_extension(record.event, "context") is not False)
        messages = build_messages(context_records, self.context)
        if inspect.isawaitable(messages):
            messages = await messages
        return messages

    async def handoff(
        self,
        *,
        name: str,
        state: dict[str, Any] | None = None,
        **extensions: Any,
    ) -> TapeRecord:
        """Commit one context anchor representing the handoff transition."""

        return await self.append(anchor_event(name, state=state, **extensions))

    async def record_chat(
        self,
        *,
        model_call_id: str,
        system_prompt: str | None,
        new_messages: list[dict[str, Any]],
        response_text: str | None,
        llm_call_count: int = 1,
        context_error: BubError | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_results: list[Any] | None = None,
        error: BubError | None = None,
        model: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        tape_name = self.name
        extensions = correlation_extensions(self.context.state, model_call_id=model_call_id)
        if system_prompt:
            await self.append(
                message_event({"role": "system", "content": system_prompt}, context=False, **extensions),
                tape=tape_name,
            )
        if context_error is not None:
            await self.append(error_event(context_error, **extensions), tape=tape_name)
        for message in new_messages:
            await self.append(message_event(message, **extensions), tape=tape_name)
        if error is not None and error is not context_error:
            await self.append(error_event(error, **extensions), tape=tape_name)
        if response_text is not None or tool_calls or tool_results is not None:
            await self.append(
                message_event(
                    {"role": "assistant", "content": response_text or ""},
                    llm_call_count=llm_call_count,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    model=model,
                    usage=usage,
                    **extensions,
                ),
                tape=tape_name,
            )

    async def _archive_tape(self, tape_name: str, stamp: str) -> Path:
        if self.archiver is None:
            raise RuntimeError("tape archiving is not configured")
        return await self.archiver.archive(tape_name, self.store, stamp)

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
            archive_path = await self._archive_tape(self.sidecar_tape_name(sidecar.name), archive_stamp)
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
        tape_name = self.sidecar_tape_name(sidecar.name)
        try:
            await self.store.reset(tape_name)
        except Exception as exc:
            return self._sidecar_lifecycle_data(sidecar=sidecar.name, status="error", reason=reason, error=exc)
        self._feed.reset(tape_name)
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
        await self.append_event("sidecar.archive", event_data)
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
            await self.append_event("sidecar.archive", archive_data)
        await self.append_event("sidecar.reset", reset_data)

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
            for sidecar in self._sibling_sidecars():
                _, sidecar_archive = await self._try_archive_sidecar(sidecar, reason="tape.reset", stamp=stamp)
                sidecar_archives[sidecar.name] = sidecar_archive
        await self.store.reset(self.name)
        self._feed.reset(self.name)
        state = {"owner": "human"}
        if archive_path is not None:
            state["archived"] = str(archive_path)
        await self.handoff(name="session/start", state=state)
        for sidecar in self._sibling_sidecars():
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
                await self.append_event("sidecar.archive", archive_data)
            await self.append_event("sidecar.reset", reset_data)
        return f"Archived: {archive_path}" if archive_path else "ok"

    def session_tape(self, session_id: str, workspace: Path, context: TapeContext | None = None) -> Tape:
        workspace_hash = hashlib.md5(str(workspace.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        tape_name = (
            workspace_hash + "__" + hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
        )
        return self.scoped(tape_name, context=context)

    @contextlib.asynccontextmanager
    async def fork_tape(self, merge_back: bool = True) -> AsyncGenerator[Tape, None]:
        managed_sidecars = self._sibling_tapes()
        overlay_store: Any = self.store
        overlays: list[Any] = []
        for sidecar in self.sidecars:
            if not isinstance(sidecar, TapeOverlaySidecar):
                continue
            overlay_store = sidecar.create_overlay(overlay_store, self.name, managed_sidecars)
            overlays.append(overlay_store)
        if not overlays:
            raise RuntimeError("tape fork requires a TapeOverlaySidecar")

        forked = replace(self, store=overlay_store, _feed=_TapeFeed())
        try:
            yield forked
        finally:
            if merge_back:
                for overlay in reversed(overlays):
                    await overlay.merge_back()
                for tape_name in (self.name, *managed_sidecars):
                    if any(tape_name in overlay.reset_tapes for overlay in overlays):
                        self._feed.reset(tape_name)
                    else:
                        self._feed.publish(tape_name)
