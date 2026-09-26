from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import uuid
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(eq=False)
class _ShellScope:
    closed: bool = False


@dataclass(slots=True)
class ManagedShell:
    shell_id: str
    cmd: str
    cwd: str | None
    session_id: str | None
    process: asyncio.subprocess.Process
    output_chunks: list[str] = field(default_factory=list)
    read_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    scope: _ShellScope | None = None
    termination_task: asyncio.Task[ManagedShell] | None = None

    @property
    def output(self) -> str:
        return "".join(self.output_chunks)

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    @property
    def status(self) -> str:
        return "running" if self.returncode is None else "exited"


class ShellManager:
    SHELL = shutil.which("bash") or shutil.which("sh") if os.name != "nt" else None
    TERMINATE_TIMEOUT = 3.0
    DRAIN_TIMEOUT = 1.0

    def __init__(self) -> None:
        self._shells: dict[str, ManagedShell] = {}
        self._scope: ContextVar[_ShellScope | None] = ContextVar("shell_scope", default=None)
        self._starting: dict[asyncio.Task[ManagedShell], _ShellScope | None] = {}

    @contextlib.asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Own shells across turns and close them when this runtime exits."""
        scope = _ShellScope()
        token = self._scope.set(scope)
        try:
            yield
        finally:
            scope.closed = True
            try:
                await self._finish_cleanup(asyncio.create_task(self._close_scope(scope)))
            finally:
                self._scope.reset(token)

    async def _close_scope(self, scope: _ShellScope) -> None:
        await asyncio.gather(
            *(task for task, owner in self._starting.items() if owner is scope), return_exceptions=True
        )
        await self._terminate_shells([shell for shell in self._shells.values() if shell.scope is scope])

    async def start(self, *, cmd: str, cwd: str | None, session_id: str | None = None) -> ManagedShell:
        scope = self._scope.get()
        if scope is not None and scope.closed:
            raise RuntimeError("shell runtime is closed")
        task = asyncio.create_task(self._start(cmd=cmd, cwd=cwd, session_id=session_id, scope=scope))
        self._starting[task] = scope
        try:
            try:
                shell = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Spawning can finish after cancellation; retain ownership until it is cleaned up.
                try:
                    await self._finish_cleanup(task)
                finally:
                    if not task.cancelled() and task.exception() is None:
                        await self._terminate_shells([task.result()])
                raise
            if scope is not None and scope.closed:
                await self._terminate_shells([shell])
                raise RuntimeError("shell runtime is closed")
            return shell
        finally:
            self._starting.pop(task, None)

    async def _start(
        self, *, cmd: str, cwd: str | None, session_id: str | None, scope: _ShellScope | None
    ) -> ManagedShell:
        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            executable=self.SHELL,
            start_new_session=os.name != "nt",
        )
        shell = ManagedShell(
            shell_id=f"bash-{uuid.uuid4().hex[:8]}",
            cmd=cmd,
            cwd=cwd,
            session_id=session_id,
            process=process,
            scope=scope,
        )
        shell.read_tasks.extend([
            asyncio.create_task(self._drain_stream(shell, process.stdout)),
            asyncio.create_task(self._drain_stream(shell, process.stderr)),
        ])
        self._shells[shell.shell_id] = shell
        return shell

    def get(self, shell_id: str) -> ManagedShell:
        try:
            return self._shells[shell_id]
        except KeyError as exc:
            raise KeyError(f"unknown shell id: {shell_id}") from exc

    async def terminate(self, shell_id: str) -> ManagedShell:
        shell = self.get(shell_id)
        return await self._finish_cleanup(self._termination(shell))

    def _termination(self, shell: ManagedShell) -> asyncio.Task[ManagedShell]:
        if shell.termination_task is None:
            shell.termination_task = asyncio.create_task(self._terminate(shell))
        return shell.termination_task

    async def _terminate_shells(self, shells: list[ManagedShell]) -> None:
        async def cleanup() -> None:
            results = await asyncio.gather(*(self._termination(shell) for shell in shells), return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise BaseExceptionGroup("shell cleanup failed", errors)

        await self._finish_cleanup(asyncio.create_task(cleanup()))

    @staticmethod
    async def _finish_cleanup[T](task: asyncio.Task[T]) -> T:
        """Finish cleanup before propagating cancellation, including repeated cancellation."""
        cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if task.cancelled():
                    raise
                cancelled = exc
        result = task.result()
        if cancelled is not None:
            raise cancelled
        return result

    async def _terminate(self, shell: ManagedShell) -> ManagedShell:
        self._signal_shell(shell, kill=False)
        try:
            async with asyncio.timeout(self.TERMINATE_TIMEOUT):
                # The shell may exit before its children, even when they have
                # closed their output pipes. Wait for the group, not just its leader.
                while self._is_running(shell):
                    await asyncio.sleep(0.05)
        except TimeoutError:
            self._signal_shell(shell, kill=True)
        try:
            async with asyncio.timeout(self.DRAIN_TIMEOUT):
                await shell.process.wait()
                await asyncio.gather(*shell.read_tasks)
        except TimeoutError:
            pass
        finally:
            # A descendant can escape the group and retain a pipe. Do not let
            # waiting for EOF make termination unbounded.
            for task in shell.read_tasks:
                task.cancel()
            await asyncio.gather(*shell.read_tasks, return_exceptions=True)
            self._shells.pop(shell.shell_id, None)
        return shell

    @staticmethod
    def _signal_shell(shell: ManagedShell, *, kill: bool) -> None:
        with contextlib.suppress(ProcessLookupError):
            if os.name != "nt":
                os.killpg(shell.process.pid, signal.SIGKILL if kill else signal.SIGTERM)
            elif shell.returncode is None:
                if kill:
                    shell.process.kill()
                else:
                    shell.process.terminate()

    @staticmethod
    def _is_running(shell: ManagedShell) -> bool:
        if os.name == "nt":
            return shell.returncode is None
        try:
            os.killpg(shell.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # EPERM does not establish that the group is gone (in particular
            # while its leader is exiting on macOS).
            return True
        return True

    async def terminate_session(self, session_id: str) -> int:
        shells = [
            shell
            for shell in self._shells.values()
            if shell.session_id == session_id and shell.scope is self._scope.get()
        ]
        await self._terminate_shells(shells)
        return len(shells)

    async def wait_closed(self, shell_id: str) -> ManagedShell:
        shell = self.get(shell_id)
        try:
            if shell.returncode is None:
                await shell.process.wait()
            for task in shell.read_tasks:
                # A foreground timeout must leave readers running for background output.
                await asyncio.shield(task)
        except Exception:
            await self._terminate_shells([shell])
            raise
        if shell.termination_task is not None:
            return await self._finish_cleanup(shell.termination_task)
        if self._is_running(shell):
            # The leader and its pipes can exit while redirected children remain alive.
            return await self.terminate(shell_id)
        self._shells.pop(shell.shell_id, None)
        return shell

    async def _drain_stream(
        self,
        shell: ManagedShell,
        stream: asyncio.StreamReader | None,
    ) -> None:
        if stream is None:
            return
        while chunk := await stream.read(4096):
            shell.output_chunks.append(chunk.decode("utf-8", errors="replace"))


shell_manager = ShellManager()
