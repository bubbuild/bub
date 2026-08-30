from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bub.sidecars import ForkOverlaySidecar
from bub.store import ForkTapeStore, InMemoryTapeStore, TapeQuery
from bub.tape import Tape, TapeContext, bub_event_type, event_extension, event_payload, message_event


async def _entries(store: InMemoryTapeStore, tape: str) -> list:
    return [entry async for entry in store.scan(TapeQuery(tape))]


@pytest.mark.asyncio
async def test_tape_fork_binds_temporary_fork_store_to_scoped_tape(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = Tape(parent, TapeContext(), sidecars=(ForkOverlaySidecar(),)).scoped("test-tape")

    async with root.fork_tape(merge_back=True) as forked:
        first_store = forked.store

        assert isinstance(first_store, ForkTapeStore)
        assert first_store is not root.store

        await forked.append_event("step", {"value": 1})
        assert await _entries(parent, "test-tape") == []

    assert [record.event.get_type() for record in await _entries(parent, "test-tape")] == [bub_event_type("step")]

    async with root.fork_tape(merge_back=False) as forked:
        second_store = forked.store
        await forked.append_event("step", {"value": 2})

    assert isinstance(second_store, ForkTapeStore)
    assert second_store is not first_store
    assert [event_payload(record.event)["value"] for record in await _entries(parent, "test-tape")] == [1]


@pytest.mark.asyncio
async def test_tape_info_reports_last_token_cache_hit_rate(tmp_path: Path) -> None:
    tape = Tape(InMemoryTapeStore(), TapeContext()).scoped("test-tape")
    await tape.record_chat(
        model_call_id="model-1",
        system_prompt=None,
        new_messages=[],
        response_text="done",
        model="test-model",
        usage={
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 60},
        },
    )

    info = await tape.info()

    assert info.last_token_usage == 100
    assert info.last_token_cache_hit_rate == 0.75


@pytest.mark.asyncio
async def test_tape_info_omits_cache_hit_rate_when_usage_has_no_cache_details(tmp_path: Path) -> None:
    tape = Tape(InMemoryTapeStore(), TapeContext()).scoped("test-tape")
    await tape.record_chat(
        model_call_id="model-1",
        system_prompt=None,
        new_messages=[],
        response_text="done",
        model="test-model",
        usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
    )

    info = await tape.info()

    assert info.last_token_cache_hit_rate is None


@pytest.mark.asyncio
async def test_context_excluded_entries_do_not_reach_custom_context_selectors(tmp_path: Path) -> None:
    def select_events(records, _context):
        return [{"role": "assistant", "content": record.event.get_type()} for record in records]

    tape = Tape(
        InMemoryTapeStore(),
        TapeContext(anchor=None, select=select_events),
    ).scoped("test-tape")
    await tape.append(message_event({"role": "user", "content": "visible"}))
    await tape.append_event("diagnostic", {})

    assert await tape.read_messages() == [{"role": "assistant", "content": bub_event_type("message")}]


@pytest.mark.asyncio
async def test_handoff_commits_exactly_one_anchor_fact() -> None:
    tape = Tape(InMemoryTapeStore(), TapeContext()).scoped("test-tape")

    committed = await tape.handoff(name="phase/two", state={"owner": "agent"})

    assert [record async for record in tape.stream()] == [committed]
    assert committed.event.get_type() == bub_event_type("context.anchor")
    assert event_payload(committed.event) == {"name": "phase/two", "state": {"owner": "agent"}}


@pytest.mark.asyncio
async def test_runtime_operation_is_durable_but_excluded_from_context(tmp_path: Path) -> None:
    def select_events(records, _context):
        return [{"role": "assistant", "content": record.event.get_type()} for record in records]

    tape = Tape(InMemoryTapeStore(), TapeContext(anchor=None, select=select_events)).scoped("test-tape")

    operation = await tape.record_operation("model", "started", {"model": "openai:test"})

    assert [entry async for entry in tape.stream()] == [operation]
    assert event_extension(operation.event, "context") is False
    assert await tape.read_messages() == []


@pytest.mark.asyncio
async def test_committed_observer_failure_does_not_roll_back_entry(tmp_path: Path) -> None:
    class BrokenObserver:
        name = "broken"

        async def on_commit(self, tape: str, entry) -> None:
            raise RuntimeError("observer unavailable")

    tape = Tape(InMemoryTapeStore(), TapeContext(anchor=None), sidecars=(BrokenObserver(),)).scoped("test-tape")

    committed = await tape.append_event("diagnostic", {"ok": True})

    assert [entry async for entry in tape.stream()] == [committed]


@pytest.mark.asyncio
async def test_following_stream_yields_only_committed_entries_and_resumes_by_cursor() -> None:
    tape = Tape(InMemoryTapeStore(), TapeContext()).scoped("test-tape")
    stream = tape.stream(follow=True)
    first = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    committed = await tape.append_event("step", {"value": 1})

    assert await asyncio.wait_for(first, timeout=1) == committed
    assert committed.cursor == 1
    second = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await tape.append_event("step", {"value": 2})
    assert (await asyncio.wait_for(second, timeout=1)).cursor == 2
    await stream.aclose()


@pytest.mark.asyncio
async def test_following_stream_catches_up_when_notifications_coalesce() -> None:
    tape = Tape(InMemoryTapeStore(), TapeContext()).scoped("test-tape")
    stream = tape.stream(follow=True)
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    await tape.append_event("step", {"value": 1})
    await tape.append_event("step", {"value": 2})

    first = await asyncio.wait_for(waiting, timeout=1)
    second = await asyncio.wait_for(anext(stream), timeout=1)
    assert [event_payload(first.event)["value"], event_payload(second.event)["value"]] == [1, 2]
    await stream.aclose()


@pytest.mark.asyncio
async def test_following_stream_restarts_after_tape_reset() -> None:
    tape = Tape(InMemoryTapeStore(), TapeContext()).scoped("test-tape")
    await tape.append_event("old", {"value": 1})
    await tape.append_event("old", {"value": 2})
    stream = tape.stream(tape.query().after(2), follow=True)
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)

    await tape.reset()

    first_after_reset = await asyncio.wait_for(waiting, timeout=1)
    assert first_after_reset.cursor == 1
    assert first_after_reset.event.get_type() == bub_event_type("context.anchor")
    await stream.aclose()
