"""Host implementation of :class:`bub.environment.Environment`, provided by Bub's builtin hooks."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bub.environment import ENVIRONMENT_STATE_KEY, CallTool, Environment, Process

if TYPE_CHECKING:
    from bub.turn import TurnState


class LocalProcess(Process):
    """A host process in its own session, so signals reach the whole process group."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def stdout(self) -> asyncio.StreamReader:
        assert self.process.stdout is not None  # noqa: S101
        return self.process.stdout

    @property
    def stderr(self) -> asyncio.StreamReader:
        assert self.process.stderr is not None  # noqa: S101
        return self.process.stderr

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def wait(self) -> int:
        return await self.process.wait()

    async def write_stdin(self, data: bytes) -> None:
        assert self.process.stdin is not None  # noqa: S101
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    def close_stdin(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()

    def signal(self, *, kill: bool) -> None:
        with contextlib.suppress(ProcessLookupError):
            if os.name != "nt":
                os.killpg(self.process.pid, signal.SIGKILL if kill else signal.SIGTERM)
            elif self.returncode is None:
                if kill:
                    self.process.kill()
                else:
                    self.process.terminate()

    def is_running(self) -> bool:
        if os.name == "nt":
            return self.returncode is None
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # EPERM does not establish that the group is gone (in particular
            # while its leader is exiting on macOS).
            return True
        return True


class LocalEnvironment(Environment):
    """Run processes and access files directly on the host."""

    SHELL = shutil.which("bash") or shutil.which("sh") if os.name != "nt" else None

    def __init__(self, workspace: str | Path | None = None) -> None:
        self.workspace = str(workspace) if workspace is not None else None
        self.python = sys.executable

    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> LocalProcess:
        options: dict[str, Any] = {
            "cwd": cwd or self.workspace,
            "env": {**os.environ, **env} if env else None,
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "start_new_session": os.name != "nt",
        }
        if isinstance(command, str):
            process = await asyncio.create_subprocess_shell(command, executable=self.SHELL, **options)
        else:
            process = await asyncio.create_subprocess_exec(*command, **options)
        return LocalProcess(process)

    async def read_text(self, path: str) -> str:
        return await asyncio.to_thread(Path(path).read_text, encoding="utf-8")

    async def read_bytes(self, path: str) -> bytes:
        return await asyncio.to_thread(Path(path).read_bytes)

    async def write_text(self, path: str, content: str) -> None:
        def write() -> None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        await asyncio.to_thread(write)

    async def run_code(
        self, code: str, *, tools: Sequence[str], call_tool: CallTool, write: Callable[[str], None]
    ) -> None:
        from bub.builtin.codemode.code_runner import run_code_in_subprocess

        await run_code_in_subprocess(self, code, tools=tools, call_tool=call_tool, write=write)

    def resolve_path(self, path: str) -> str:
        expanded = Path(path).expanduser()
        if expanded.is_absolute():
            return str(expanded)
        if self.workspace is None:
            raise ValueError(f"relative path '{path}' is not allowed without a workspace")
        return str((Path(self.workspace) / expanded).resolve())


def environment_from_state(state: TurnState) -> Environment:
    """Return the session's environment, or a host environment for the state's workspace when none was provided.

    The fallback covers tools run outside a framework turn, for example by SDK callers or tests.
    """
    environment = state.get(ENVIRONMENT_STATE_KEY)
    if isinstance(environment, Environment):
        return environment
    workspace = state.get("_runtime_workspace")
    if workspace is not None and not isinstance(workspace, str | Path):
        raise TypeError("runtime workspace must be a filesystem path")
    return LocalEnvironment(workspace)
