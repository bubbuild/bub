"""Trusted local implementation. Distinct workspaces are not security isolation."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from bub.environment import ExecutionEnvironment

if TYPE_CHECKING:
    from bub.tools import Tool, ToolContext


class LocalExecutionEnvironment:
    def __init__(self, workspace: str | Path | None = None, session_id: str = "") -> None:
        self.workspace = Path(workspace).resolve() if workspace else None
        self.session_id = session_id
        self._handles: set[str] = set()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[LocalExecutionEnvironment]:
        """Local resources need no allocation; background shells outlive turns."""
        yield self

    @property
    def render_context(self) -> Mapping[str, str]:
        return {"PYTHON": sys.executable}

    async def map_resource(self, source: Path) -> str:
        target = source.resolve(strict=True)
        if not (target.is_file() or target.is_dir()):
            raise ValueError(f"Unsupported resource: {source}")
        return str(target)

    def _path(self, path: str) -> Path:
        target = Path(path).expanduser()
        if target.is_absolute():
            return target
        if self.workspace is None:
            raise ValueError(f"relative path '{path}' is not allowed without a workspace")
        return (self.workspace / target).resolve()

    async def _read_file(self, path: str) -> str:
        return self._path(path).read_text(encoding="utf-8")

    async def _write_file(self, path: str, content: str) -> str:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)

    def _check_handle(self, shell_id: str) -> None:
        if shell_id not in self._handles:
            raise KeyError(f"shell does not belong to this environment: {shell_id}")

    async def stop(self) -> None:
        from bub.builtin.tools import shell_manager

        for handle in tuple(self._handles):
            try:
                shell_manager.get(handle)
            except KeyError:
                continue
            await shell_manager.terminate(handle)

    async def has_active_processes(self) -> bool:
        from bub.builtin.tools import shell_manager

        for handle in self._handles:
            try:
                if shell_manager.get(handle).returncode is None:
                    return True
            except KeyError:
                continue
        return False

    def bind_tools(self, tools: Mapping[str, Tool]) -> Mapping[str, Tool]:
        return tools


def environment_for(context: ToolContext) -> ExecutionEnvironment:
    environment: ExecutionEnvironment | None = context.state.get("_runtime_execution_environment")
    if environment is not None:
        return environment
    return LocalExecutionEnvironment(context.state.get("_runtime_workspace"), str(context.state.get("session_id", "")))


def bind_turn_tools(tools: Mapping[str, Tool], environment: ExecutionEnvironment) -> dict[str, Tool]:
    """Select builtin execution capabilities; host tools retain their handlers."""
    required = {
        name: tool
        for name, tool in tools.items()
        if name in {"bash", "bash.output", "bash.kill", "fs.read", "fs.write", "fs.edit", "run_code"}
    }
    bound = environment.bind_tools(required)
    if set(bound) != set(required):
        raise ValueError("Environment must bind every required execution tool")
    return {**tools, **bound}


def local_environment(context: ToolContext | None) -> LocalExecutionEnvironment | None:
    """Local handlers must never silently execute for a remote binding."""
    environment = context.state.get("_runtime_execution_environment") if context else None
    if environment is not None and not isinstance(environment, LocalExecutionEnvironment):
        raise ValueError("Local tool handler cannot execute in a non-local environment")
    return environment
