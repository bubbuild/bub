"""Execution environments for tools that run processes or touch files.

A :class:`Sandbox` provides a few low-level capabilities — spawn a process,
read and write text files, resolve paths — and tools such as ``bash`` and
``fs.*`` run on top of it. Tool semantics (background shells, timeouts,
rendering, hooks) stay on the host; only execution moves into the sandbox.
Plugins provide one per session through the ``provide_sandbox`` hook; the
default :class:`LocalSandbox` runs everything on the host.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import os
import shutil
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bub.turn import TurnState

SANDBOX_STATE_KEY = "_runtime_sandbox"


class SandboxProcess(abc.ABC):
    """A process started by a sandbox, with piped stdin, stdout and stderr."""

    @property
    @abc.abstractmethod
    def stdout(self) -> asyncio.StreamReader:
        """The process's standard output."""

    @property
    @abc.abstractmethod
    def stderr(self) -> asyncio.StreamReader:
        """The process's standard error."""

    @property
    @abc.abstractmethod
    def returncode(self) -> int | None:
        """The exit code, or ``None`` while the process is running."""

    @abc.abstractmethod
    async def wait(self) -> int:
        """Wait for the process to exit and return its exit code."""

    @abc.abstractmethod
    async def write_stdin(self, data: bytes) -> None:
        """Write to the process's standard input and wait until it is flushed."""

    @abc.abstractmethod
    def close_stdin(self) -> None:
        """Close the process's standard input."""

    @abc.abstractmethod
    def signal(self, *, kill: bool) -> None:
        """Ask the process and its descendants to stop, or kill them when ``kill`` is true.

        Must not raise when the processes have already exited.
        """

    def is_running(self) -> bool:
        """Whether the process or any descendant it started is still alive.

        A shell can exit before the children it started; the default only checks the process itself.
        """
        return self.returncode is None


class Sandbox(abc.ABC):
    """An environment where tools run processes and access files.

    Paths passed to a sandbox are paths inside the sandbox; ``workspace`` is the
    working directory that relative paths resolve against.
    """

    workspace: str | None = None
    """The default working directory inside the sandbox, if any."""

    python: str = "python3"
    """The Python executable inside the sandbox."""

    @abc.abstractmethod
    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> SandboxProcess:
        """Start a process: a string runs through the shell, a sequence runs as an argument vector.

        ``cwd`` defaults to ``workspace``; ``env`` adds to the sandbox's default environment.
        """

    @abc.abstractmethod
    async def read_text(self, path: str) -> str:
        """Read a UTF-8 text file."""

    @abc.abstractmethod
    async def write_text(self, path: str, content: str) -> None:
        """Write a UTF-8 text file, creating missing parent directories."""

    def resolve_path(self, path: str) -> str:
        """Resolve ``path`` against ``workspace``; relative paths require a workspace."""
        import posixpath

        if posixpath.isabs(path):
            return path
        if self.workspace is None:
            raise ValueError(f"relative path '{path}' is not allowed without a workspace")
        return posixpath.normpath(posixpath.join(self.workspace, path))

    async def aclose(self) -> None:  # noqa: B027
        """Release the sandbox's resources. Called when the framework stops."""


class LocalProcess(SandboxProcess):
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


class LocalSandbox(Sandbox):
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

    async def write_text(self, path: str, content: str) -> None:
        def write() -> None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        await asyncio.to_thread(write)

    def resolve_path(self, path: str) -> str:
        expanded = Path(path).expanduser()
        if expanded.is_absolute():
            return str(expanded)
        if self.workspace is None:
            raise ValueError(f"relative path '{path}' is not allowed without a workspace")
        return str((Path(self.workspace) / expanded).resolve())


def sandbox_from_state(state: TurnState) -> Sandbox:
    """Return the session's sandbox, or a host sandbox for the state's workspace when none was provided."""
    sandbox = state.get(SANDBOX_STATE_KEY)
    if isinstance(sandbox, Sandbox):
        return sandbox
    workspace = state.get("_runtime_workspace")
    if workspace is not None and not isinstance(workspace, str | Path):
        raise TypeError("runtime workspace must be a filesystem path")
    return LocalSandbox(workspace)
