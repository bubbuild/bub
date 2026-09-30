"""Environment-owned filesystem, processes and code; Tape and loop stay on the host."""

from __future__ import annotations

from collections.abc import Mapping
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
