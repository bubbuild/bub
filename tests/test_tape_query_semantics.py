"""Behavioral boundaries for query views, context projection, and fork merges."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

import pytest
import republic

from bub.core.context import default_tape_context
from bub.core.store import ForkTapeStore, InMemoryTapeStore, TapeQuery, TapeRecord
from bub.core.tape import Tape, TapeContext, TapeEntry
from bub.errors import BubError, ErrorKind


def event_names(entries: Iterable[TapeEntry]) -> list[str]:
    return [str(entry.payload["name"]) for entry in entries]


@pytest.mark.asyncio
async def test_fork_queries_observe_later_parent_appends_before_local_entries() -> None:
    parent = InMemoryTapeStore()
    (await parent.append("main", TapeEntry.event("parent-first")))
    fork = ForkTapeStore(parent, "main")
    await fork.append("main", TapeEntry.event("local"))
    query = TapeQuery("main", fork)

    assert event_names(await query.all()) == ["parent-first", "local"]
    (await parent.append("main", TapeEntry.event("parent-later")))
    assert event_names(await query.all()) == ["parent-first", "parent-later", "local"]


@pytest.mark.asyncio
async def test_local_named_anchor_shadows_parent_and_local_prefix() -> None:
    parent = InMemoryTapeStore()
    (await parent.append("main", TapeEntry.anchor("phase")))
    (await parent.append("main", TapeEntry.event("parent-after")))
    fork = ForkTapeStore(parent, "main")
    await fork.append("main", TapeEntry.event("local-before"))
    await fork.append("main", TapeEntry.anchor("phase"))
    await fork.append("main", TapeEntry.event("local-after"))

    assert event_names(await TapeQuery("main", fork).after_anchor("phase").all()) == ["local-after"]


@pytest.mark.asyncio
async def test_fork_body_failure_still_merges_by_default() -> None:
    parent = InMemoryTapeStore()
    root = Tape(parent, TapeContext()).scoped("main")

    with pytest.raises(RuntimeError, match="body failed"):
        async with root.fork_tape() as forked:
            await forked.append_event("before-error", {})
            raise RuntimeError("body failed")

    assert event_names((await TapeQuery("main", parent).all()) or []) == ["before-error"]


@pytest.mark.asyncio
async def test_discard_fork_does_not_rollback_unmanaged_tape_writes() -> None:
    parent = InMemoryTapeStore()
    root = Tape(parent, TapeContext()).scoped("main")
    async with root.fork_tape(merge_back=False) as forked:
        await forked.append_event("discard-me", {})
        await forked.scoped("other").append_event("persist-me", {})

    assert (await TapeQuery("main", parent).all()) == []
    assert event_names((await TapeQuery("other", parent).all()) or []) == ["persist-me"]


@pytest.mark.asyncio
async def test_nested_fork_merges_into_immediate_parent_not_root() -> None:
    parent = InMemoryTapeStore()
    root = Tape(parent, TapeContext()).scoped("main")
    async with root.fork_tape(merge_back=False) as outer:
        await outer.append_event("outer", {})
        async with outer.fork_tape() as inner:
            await inner.append_event("inner", {})
        assert event_names(await outer.query().all()) == ["outer", "inner"]
        assert (await TapeQuery("main", parent).all()) == []

    assert (await TapeQuery("main", parent).all()) == []


@pytest.mark.asyncio
async def test_named_anchor_uses_last_occurrence_and_between_excludes_both_anchors() -> None:
    store = InMemoryTapeStore()
    for entry in (
        TapeEntry.anchor("start"),
        TapeEntry.event("old"),
        TapeEntry.anchor("end"),
        TapeEntry.anchor("start"),
        TapeEntry.event("new"),
        TapeEntry.anchor("end"),
        TapeEntry.event("tail"),
    ):
        (await store.append("main", entry))

    query = TapeQuery("main", store)
    assert event_names(await query.between_anchors("start", "end").all()) == ["new"]
    assert event_names(await query.after_anchor("start").kinds("event").all()) == ["new", "tail"]


@pytest.mark.asyncio
async def test_view_does_not_reinsert_boundary_anchor_or_its_state() -> None:
    store = InMemoryTapeStore()
    context = default_tape_context()
    tape = Tape(store, context).scoped("main")
    (await store.append("main", TapeEntry.anchor("phase", {"summary": "not injected"})))
    (await store.append("main", TapeEntry.message({"role": "user", "content": "visible"})))

    assert await tape.read_messages() == [republic.user("visible")]
    all_messages = await tape.with_context(replace(context, anchor=None)).read_messages()
    assert len(all_messages) == 2
    assert "[Anchor created: phase]" in all_messages[0].text
    assert "not injected" in all_messages[0].text


@pytest.mark.asyncio
async def test_async_context_selector_keeps_iterable_input_and_filters_only_false() -> None:
    seen: list[str] = []

    async def select(entries: Iterable[TapeEntry], context: TapeContext) -> list[republic.Message]:
        assert context.state == {"key": "value"}
        seen.extend(event_names(entries))
        return [republic.assistant(",".join(seen))]

    parent = InMemoryTapeStore()
    tape = Tape(
        parent,
        TapeContext(anchor=None, select=select, state={"key": "value"}),
    ).scoped("main")
    await tape.append_event("visible", {})
    await tape.append_event("hidden", {}, context=False)
    await tape.append_event("zero", {}, context=0)

    assert await tape.read_messages() == [republic.assistant("visible,zero")]
    assert event_names(await tape.query().all()) == ["visible", "hidden", "zero"]


@pytest.mark.asyncio
async def test_fork_can_read_local_entries_when_parent_has_no_anchor() -> None:
    parent = InMemoryTapeStore()
    (await parent.append("main", TapeEntry.event("parent")))
    with pytest.raises(BubError) as caught:
        await TapeQuery("main", parent).last_anchor().all()
    assert caught.value.kind is ErrorKind.NOT_FOUND

    fork = ForkTapeStore(parent, "main")
    await fork.append("main", TapeEntry.event("local-without-anchor"))
    assert event_names(await TapeQuery("main", fork).last_anchor().all()) == ["local-without-anchor"]


@pytest.mark.asyncio
async def test_sidecar_merge_failure_records_error_and_continues_before_main() -> None:
    attempts: list[str] = []

    class Parent(InMemoryTapeStore):
        async def append(self, tape: str, entry: TapeEntry) -> TapeRecord:
            attempts.append(tape)
            if tape == "bad-sidecar":
                raise RuntimeError("sidecar unavailable")
            return await super().append(tape, entry)

    parent = Parent()
    fork = ForkTapeStore(parent, "main", sidecars=("bad-sidecar", "good-sidecar"))
    await fork.append("main", TapeEntry.event("main-data"))
    await fork.append("bad-sidecar", TapeEntry.event("bad-data"))
    await fork.append("good-sidecar", TapeEntry.event("good-data"))
    await fork.merge_back()

    assert attempts == ["bad-sidecar", "good-sidecar", "main", "main"]
    main = (await TapeQuery("main", parent).all()) or []
    assert event_names(main) == ["main-data", "sidecar.merge"]
    assert main[-1].meta["context"] is False
    assert main[-1].payload["data"]["status"] == "error"
    assert event_names((await TapeQuery("good-sidecar", parent).all()) or []) == ["good-data"]


@pytest.mark.asyncio
async def test_main_merge_failure_propagates_without_rolling_back_sidecar() -> None:
    class Parent(InMemoryTapeStore):
        async def append(self, tape: str, entry: TapeEntry) -> TapeRecord:
            if tape == "main":
                raise RuntimeError("main unavailable")
            return await super().append(tape, entry)

    parent = Parent()
    fork = ForkTapeStore(parent, "main", sidecars=("sidecar",))
    await fork.append("sidecar", TapeEntry.event("persisted"))
    await fork.append("main", TapeEntry.event("failed"))
    with pytest.raises(RuntimeError, match="main unavailable"):
        await fork.merge_back()

    assert event_names((await TapeQuery("sidecar", parent).all()) or []) == ["persisted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "file"])
async def test_query_anchor_precedence_dates_text_kinds_and_limits(tmp_path, backend) -> None:
    from bub.core.store import FileTapeStore

    store = InMemoryTapeStore() if backend == "memory" else FileTapeStore(tmp_path)
    for entry in (
        TapeEntry.anchor("start"),
        TapeEntry(0, "event", {"name": "old"}, date="2026-01-01"),
        TapeEntry.anchor("end"),
        TapeEntry.anchor("start"),
        TapeEntry(0, "event", {"name": "Strasse"}, date="2026-01-02T00:00:00"),
        TapeEntry(0, "event", {"name": "STRASSE"}, date="2026-01-02T23:59:59.999999Z"),
        TapeEntry(0, "message", {"content": "strasse"}, date="2026-01-02"),
        TapeEntry.anchor("end"),
        TapeEntry(0, "event", {"name": "tail"}, date="2026-01-03"),
    ):
        await store.append("main", entry)
    query = TapeQuery("main", store).between_anchors("start", "end").last_anchor().after_anchor("")
    query = query.between_dates("2026-01-02", "2026-01-02").query("STRASSE").kinds("event")
    assert event_names(await query.all()) == ["Strasse", "STRASSE"]
    assert event_names(await query.limit(-1).all()) == ["Strasse"]
    assert event_names(await query.limit(1).all()) == ["Strasse"]
    assert await query.limit(0).all() == []
    assert query.spec.limit is None


@pytest.mark.asyncio
async def test_between_anchors_never_falls_back_to_an_earlier_complete_pair() -> None:
    store = InMemoryTapeStore()
    for name in ("start", "end", "start"):
        await store.append("main", TapeEntry.anchor(name))
    with pytest.raises(BubError) as caught:
        await TapeQuery("main", store).between_anchors("start", "end").all()
    assert caught.value.kind is ErrorKind.NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "file"])
async def test_date_errors_outside_the_selected_range_are_ignored(tmp_path, backend) -> None:
    from bub.core.store import FileTapeStore

    store = InMemoryTapeStore() if backend == "memory" else FileTapeStore(tmp_path)
    await store.append("main", TapeEntry(0, "event", {"name": "bad-date"}, date="invalid"))
    await store.append("main", TapeEntry.anchor("cut"))
    await store.append("main", TapeEntry(0, "event", {"name": "good"}, date="2026-01-01"))
    query = TapeQuery("main", store).between_dates("2026-01-01", "2026-01-02")
    with pytest.raises(BubError, match="Invalid ISO"):
        await query.all()
    assert await query.limit(0).all() == []
    assert event_names(await query.after_anchor("cut").all()) == ["good"]
    fork = ForkTapeStore(store, "main")
    await fork.append("main", TapeEntry.event("local"))
    with pytest.raises(BubError, match="Invalid ISO"):
        await TapeQuery("main", fork).between_dates("2026-01-01", "2026-01-02").all()


@pytest.mark.asyncio
async def test_fork_propagates_parent_iteration_errors_and_cancellation() -> None:
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from bub.core.store import TapeQuerySpec

    closed: list[str] = []

    class Parent(InMemoryTapeStore):
        @asynccontextmanager
        async def select(self, tape: str, spec: TapeQuerySpec) -> AsyncIterator[AsyncIterator[TapeEntry]]:
            async def entries() -> AsyncIterator[TapeEntry]:
                raise RuntimeError("iteration failed")
                yield

            try:
                if spec.text == "cancel":
                    raise asyncio.CancelledError
                yield entries()
            finally:
                closed.append(tape)

    fork = ForkTapeStore(Parent(), "main")
    await fork.append("main", TapeEntry.event("local"))
    with pytest.raises(RuntimeError, match="iteration failed"):
        await TapeQuery("main", fork).all()
    with pytest.raises(asyncio.CancelledError):
        await TapeQuery("main", fork).query("cancel").all()
    assert closed == ["main", "main"]


@pytest.mark.asyncio
async def test_fork_preserves_parent_fuzzy_order_and_appends_local_without_global_sort(tmp_path) -> None:
    from bub.builtin.store import FuzzyFileTapeStore

    parent = FuzzyFileTapeStore(tmp_path)
    await parent.append("main", TapeEntry.event("deployment old"))
    await parent.append("main", TapeEntry.event("deployment new"))
    fork = ForkTapeStore(parent, "main")
    await fork.append("main", TapeEntry.event("unrelated local"))
    query = TapeQuery("main", fork).query("deploymnt")
    assert event_names(await query.all()) == ["deployment new", "deployment old", "unrelated local"]
    assert event_names(await query.limit(2).all()) == ["deployment new", "deployment old"]
