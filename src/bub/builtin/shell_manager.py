from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import uuid
from dataclasses import dataclass, field


@dataclass(slots=True)
class ManagedShell:
    shell_id: str
    cmd: str
    cwd: str | None
    session_id: str | None
    process: asyncio.subprocess.Process
    output_chunks: list[str] = field(default_factory=list)
    read_tasks: list[asyncio.Task[None]] = field(default_factory=list)

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

    async def start(self, *, cmd: str, cwd: str | None, session_id: str | None = None) -> ManagedShell:
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

    def release(self, shell_id: str) -> ManagedShell | None:
        return self._shells.pop(shell_id, None)

    async def terminate(self, shell_id: str) -> ManagedShell:
        shell = self.get(shell_id)
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
                await self.wait_closed(shell_id)
        except TimeoutError:
            # A descendant can escape the group and retain a pipe. Do not let
            # waiting for EOF make termination unbounded.
            for task in shell.read_tasks:
                task.cancel()
            await asyncio.gather(*shell.read_tasks, return_exceptions=True)
            self._shells.pop(shell_id, None)
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
        shell_ids = [shell.shell_id for shell in self._shells.values() if shell.session_id == session_id]
        for shell_id in shell_ids:
            with contextlib.suppress(KeyError):
                await self.terminate(shell_id)
        return len(shell_ids)

    async def wait_closed(self, shell_id: str) -> ManagedShell:
        shell = self.get(shell_id)
        if shell.returncode is None:
            await shell.process.wait()
        await self._finalize_shell(shell)
        return shell

    async def _finalize_shell(self, shell: ManagedShell) -> None:
        for task in shell.read_tasks:
            # A caller timing out must not cancel a reader or swallow cancellation.
            await asyncio.shield(task)
        self._shells.pop(shell.shell_id, None)

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
