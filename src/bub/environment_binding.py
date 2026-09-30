"""Session ownership of retained execution environments and turn bindings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from bub.execution import ExecutionEnvironment


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
