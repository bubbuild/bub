"""Execution environments for tools that run processes or touch files.

A :class:`Environment` provides a few low-level capabilities — spawn a process,
read and write text files, resolve paths — and tools such as ``bash`` and
``fs.*`` run on top of it. Tool semantics (background shells, timeouts,
rendering, hooks) stay on the host; only execution moves into the environment.
Plugins provide one per session through the ``provide_environment`` hook; Bub's
builtin hooks provide ``bub.builtin.environment.LocalEnvironment``, which runs
everything on the host.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Mapping, Sequence

ENVIRONMENT_STATE_KEY = "_runtime_environment"


class Process(abc.ABC):
    """A process started by an environment, with piped stdin, stdout and stderr."""

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


class Environment(abc.ABC):
    """An environment where tools run processes and access files.

    Paths passed to an environment are paths inside the environment; ``workspace`` is the
    working directory that relative paths resolve against.
    """

    workspace: str | None = None
    """The default working directory inside the environment, if any."""

    python: str = "python3"
    """The Python executable inside the environment."""

    @abc.abstractmethod
    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> Process:
        """Start a process: a string runs through the shell, a sequence runs as an argument vector.

        ``cwd`` defaults to ``workspace``; ``env`` adds to the environment's default environment.
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
        """Release the environment's resources. Called when the framework stops."""
