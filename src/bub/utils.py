import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from bub.tape import TapeEntry
from bub.types import State


def exclude_none(d: dict[str, Any]) -> dict[str, Any]:
    """Exclude None values from a dictionary."""
    return {k: v for k, v in d.items() if v is not None}


async def wait_until_stopped[T](coro: Coroutine[None, None, T], stop_event: asyncio.Event) -> T:
    """Run a coroutine until a stop event is set."""
    task = asyncio.create_task(coro)
    waiter = asyncio.create_task(stop_event.wait())
    _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if stop_event.is_set():
        task.cancel()
        await task
        raise asyncio.CancelledError("Operation cancelled due to stop event")
    else:
        waiter.cancel()
        return task.result()


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def get_entry_text(entry: TapeEntry) -> str:
    import yaml

    return yaml.safe_dump(entry.payload)
