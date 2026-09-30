"""Execution environments for tools that run processes or touch files.

A :class:`Environment` provides a few low-level capabilities — spawn a process,
read and write text files, resolve paths, run Python code — and tools such as
``bash``, ``fs.*`` and ``run_code`` run on top of it. Tool semantics (background shells, timeouts,
rendering, hooks) stay on the host; only execution moves into the environment.
Plugins provide one per session through the ``provide_environment`` hook; Bub's
builtin hooks provide ``bub.builtin.environment.LocalEnvironment``, which runs
everything on the host.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

ENVIRONMENT_STATE_KEY = "_runtime_environment"

CallTool = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""Call a Bub tool by its code-facing name with keyword arguments; returns its structured result or raises."""


class CodeFailed(Exception):
    """Code passed to ``Environment.run_code`` raised an exception."""

    def __init__(self, error: str, traceback: str = "") -> None:
        super().__init__(error)
        self.error = error
        """The exception as ``"<Type>: <message>"``."""
        self.traceback = traceback
        """The formatted traceback, limited to frames of the code itself."""


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

    @abc.abstractmethod
    async def run_code(
        self, code: str, *, tools: Sequence[str], call_tool: CallTool, write: Callable[[str], None]
    ) -> None:
        """Run Python ``code`` and return when it finishes.

        The code may use top-level ``await``. For each name in ``tools``, ``tools.<name>(**kwargs)``
        must be an async function that returns ``await call_tool(name, kwargs)``; errors from
        ``call_tool`` surface in the code as exceptions. Pass everything the code writes to stdout
        to ``write`` as it happens, so output survives a timeout. Raise :class:`CodeFailed` when the
        code raises; any other exception reports that the runtime itself failed. When cancelled
        (for example on timeout), stop the code before returning.

        How the code runs is up to the environment: a Python process, an embedded interpreter or a
        remote code interpreter. ``bub.builtin.codemode.code_runner.run_code_in_subprocess`` implements it
        on top of :meth:`spawn` for environments that have a Python interpreter.
        """

    async def close(self) -> None:  # noqa: B027
        """Release the environment's resources. Called when the framework stops."""
