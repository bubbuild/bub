"""Execution environment contracts, session ownership and turn bindings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from bub.tools import Tool


class ExecutionEnvironment(Protocol):
    """A pinned environment instance. Providers own lifetime across turns."""

    def bind_tools(self, tools: Mapping[str, Tool]) -> Mapping[str, Tool]:
        """Bind required execution tools once per turn, or raise if unsupported.

        Return the same names and public contracts with environment-specific
        handlers. Host capabilities are not included in this collection.
        """
        ...

    @property
    def render_context(self) -> Mapping[str, str]:
        """Execution-side template values; consumers decide which keys they need."""
        ...

    async def map_resource(self, source: Path) -> str:
        """Map a host resource to an execution-side path; no implicit sync/write-back."""
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
