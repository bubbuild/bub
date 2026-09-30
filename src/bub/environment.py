"""Execution environment contracts, session ownership and turn bindings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ShellResult:
    """Transport-neutral process snapshot; output is the full buffered text."""

    shell_id: str
    output: str
    returncode: int | None
    status: str
    timed_out: bool = False


class ExecutionEnvironment(Protocol):
    """A pinned environment instance. Providers own lifetime across turns."""

    def acquire(self) -> AbstractAsyncContextManager[ExecutionEnvironment]:
        """Acquire a turn-scoped execution view, possibly self.

        Enter before capability selection. Exit releases this use,
        not background work or the retained environment. Providers must reject
        disabled handles and clean up partial acquisition on failure/cancellation.
        Returned views must not be reused after exit. Providers own coordination
        with reclamation, background work, and shared dependencies.
        """
        ...

    async def has_active_processes(self) -> bool:
        """Whether owned work prevents switching away from this binding."""
        ...

    async def stop(self) -> None:
        """Cancel owned work without disposing of the retained environment."""
        ...


@dataclass
class EnvironmentBinding:
    owner: SessionEnvironments
    active: bool = True


@dataclass
class SessionEnvironments:
    """Keep retained instances and serialization under one session owner."""

    instances: dict[str, ExecutionEnvironment] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @asynccontextmanager
    async def acquire(self, inherited: EnvironmentBinding | None = None) -> AsyncIterator[EnvironmentBinding]:
        if inherited is not None and inherited.owner is self and inherited.active:
            raise ValueError("Nested same-session execution is not supported while its environment binding is active")
        async with self._lock:
            binding = EnvironmentBinding(self)
            try:
                yield binding
            finally:
                binding.active = False
