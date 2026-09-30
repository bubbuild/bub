"""Trusted local implementation. Distinct workspaces are not security isolation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bub.environment import ExecutionEnvironment, ShellResult

if TYPE_CHECKING:
    from bub.tools import Tool, ToolContext


class LocalExecutionEnvironment:
    def __init__(
        self, workspace: str | Path | None = None, session_id: str = "", *, check_handles: bool = True
    ) -> None:
        self.workspace = Path(workspace).resolve() if workspace else None
        self.session_id = session_id
        self._handles: set[str] = set()
        self._check_handles = check_handles

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[LocalExecutionEnvironment]:
        """Local resources need no allocation; background shells outlive turns."""
        yield self

    def _path(self, path: str) -> Path:
        target = Path(path).expanduser()
        if target.is_absolute():
            return target
        if self.workspace is None:
            raise ValueError(f"relative path '{path}' is not allowed without a workspace")
        return (self.workspace / target).resolve()

    async def read_file(self, path: str) -> str:
        return self._path(path).read_text(encoding="utf-8")

    async def write_file(self, path: str, content: str) -> str:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)

    def _check_handle(self, shell_id: str) -> None:
        if self._check_handles and shell_id not in self._handles:
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

    async def start_shell(self, command: str, cwd: str | None) -> ShellResult:
        from bub.builtin.tools import shell_manager

        target = cwd or (str(self.workspace) if self.workspace else None)
        shell = await shell_manager.start(cmd=command, cwd=target, session_id=self.session_id or None)
        self._handles.add(shell.shell_id)
        return ShellResult(shell.shell_id, shell.output, shell.returncode, shell.status)

    async def read_shell(self, shell_id: str) -> ShellResult:
        from bub.builtin.tools import shell_manager

        self._check_handle(shell_id)
        shell = shell_manager.get(shell_id)
        if shell.returncode is not None:
            shell = await shell_manager.wait_closed(shell_id)
        return ShellResult(shell.shell_id, shell.output, shell.returncode, shell.status)

    async def wait_shell(self, shell_id: str, timeout_seconds: int) -> ShellResult:
        from bub.builtin.tools import shell_manager

        self._check_handle(shell_id)
        shell = shell_manager.get(shell_id)
        try:
            async with asyncio.timeout(timeout_seconds):
                shell = await shell_manager.wait_closed(shell_id)
        except TimeoutError:
            if shell.termination_task is None or not shell.termination_task.done():
                return ShellResult(shell.shell_id, shell.output, shell.returncode, shell.status, timed_out=True)
        return ShellResult(shell.shell_id, shell.output, shell.returncode, shell.status)

    async def terminate_shell(self, shell_id: str) -> ShellResult:
        from bub.builtin.tools import shell_manager

        self._check_handle(shell_id)
        shell = await shell_manager.terminate(shell_id)
        return ShellResult(shell.shell_id, shell.output, shell.returncode, shell.status)

    async def execute_code(self, code: str, callbacks: Mapping[str, Callable[..., Awaitable[Any]]]) -> str:
        from bub.builtin.codemode import _execute_local

        return await _execute_local(code, callbacks)


EXECUTION_CAPABILITIES = {
    "bash": ("start_shell", "wait_shell", "terminate_shell"),
    "bash.output": ("read_shell",),
    "bash.kill": ("terminate_shell",),
    "fs.read": ("read_file",),
    "fs.write": ("write_file",),
    "fs.edit": ("read_file", "write_file"),
    "run_code": ("execute_code",),
}


def available_turn_tools(tools: Mapping[str, Tool], environment: ExecutionEnvironment) -> dict[str, Tool]:
    """Keep shared handlers and omit unsupported builtin tools, not plugin extensions."""
    return {
        name: tool
        for name, tool in tools.items()
        if all(callable(getattr(environment, method, None)) for method in EXECUTION_CAPABILITIES.get(name, ()))
    }


def require_capability(context: ToolContext | None, method: str) -> Callable[..., Awaitable[Any]]:
    """Reject unavailable operations, including direct calls, without host fallback."""
    state = context.state if context is not None else {}
    environment = state.get("_runtime_execution_environment")
    if environment is None:
        environment = LocalExecutionEnvironment(
            state.get("_runtime_workspace"), str(state.get("session_id", "")), check_handles=False
        )
    capability = getattr(environment, method, None)
    if not callable(capability):
        raise NotImplementedError(f"Execution environment does not support {method}")
    return cast("Callable[..., Awaitable[Any]]", capability)
