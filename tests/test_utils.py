import asyncio
from pathlib import Path

import pytest

from bub.utils import exclude_none, wait_until_stopped, workspace_from_state


def test_exclude_none_keeps_non_none_values() -> None:
    payload = {"a": 1, "b": None, "c": "x", "d": False}
    assert exclude_none(payload) == {"a": 1, "c": "x", "d": False}


@pytest.mark.asyncio
async def test_wait_until_stopped_returns_result_when_coroutine_finishes_first() -> None:
    stop_event = asyncio.Event()
    result = await wait_until_stopped(asyncio.sleep(0.01, result="done"), stop_event)
    assert result == "done"


@pytest.mark.asyncio
async def test_wait_until_stopped_cancels_when_stop_event_set() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    with pytest.raises(asyncio.CancelledError):
        await wait_until_stopped(asyncio.sleep(0.2, result="done"), stop_event)


@pytest.mark.asyncio
async def test_wait_until_stopped_cancels_running_task_when_stop_event_flips() -> None:
    stop_event = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def never_finish() -> str:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise
        return "unexpected"

    async def trigger_stop() -> None:
        await asyncio.sleep(0.01)
        stop_event.set()

    trigger_task = asyncio.create_task(trigger_stop())
    with pytest.raises(asyncio.CancelledError):
        await wait_until_stopped(never_finish(), stop_event)
    await trigger_task

    assert task_cancelled.is_set()


def test_workspace_from_state_prefers_runtime_workspace_and_expands_user_home(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = Path.home().resolve()
    monkeypatch.setenv("HOME", str(expected))

    workspace = workspace_from_state({"_runtime_workspace": "~"})

    assert workspace == expected


def test_workspace_from_state_falls_back_to_current_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    workspace = workspace_from_state({"_runtime_workspace": "   "})

    assert workspace == tmp_path.resolve()


@pytest.mark.asyncio
async def test_wait_until_stopped_cleans_up_children_when_caller_is_cancelled() -> None:
    stop_event = asyncio.Event()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> str:
        started.set()
        try:
            await release.wait()
            return "done"
        finally:
            finished.set()

    runner = asyncio.create_task(wait_until_stopped(operation(), stop_event))
    await started.wait()
    runner.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert finished.is_set()
    finally:
        release.set()
        stop_event.set()
        await asyncio.sleep(0)
