from __future__ import annotations

from pathlib import Path

import pytest

from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import Tape
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeContext


@pytest.mark.asyncio
async def test_tape_fork_binds_temporary_fork_store_to_scoped_tape(tmp_path: Path) -> None:
    parent = InMemoryTapeStore()
    root = Tape(tmp_path, AsyncTapeStoreAdapter(parent), TapeContext()).scoped("test-tape")

    async with root.fork_tape(merge_back=True) as forked:
        first_store = forked.store

        assert isinstance(first_store, ForkTapeStore)
        assert first_store is not root.store

        await forked.append_event("step", {"value": 1})
        assert parent.read("test-tape") is None

    assert [entry.payload["name"] for entry in parent.read("test-tape") or []] == ["step"]

    async with root.fork_tape(merge_back=False) as forked:
        second_store = forked.store
        await forked.append_event("step", {"value": 2})

    assert isinstance(second_store, ForkTapeStore)
    assert second_store is not first_store
    assert [entry.payload["data"]["value"] for entry in parent.read("test-tape") or []] == [1]


@pytest.mark.asyncio
async def test_tape_info_reports_last_token_cache_hit_rate(tmp_path: Path) -> None:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    await tape.record_chat(
        run_id="run-1",
        system_prompt=None,
        new_messages=[],
        response_text=None,
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
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    await tape.record_chat(
        run_id="run-1",
        system_prompt=None,
        new_messages=[],
        response_text=None,
        usage={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
    )

    info = await tape.info()

    assert info.last_token_cache_hit_rate is None
