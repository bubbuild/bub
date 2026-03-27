from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field


@dataclass(slots=True)
class ManagedShell:
    shell_id: str
    cmd: str
    cwd: str | None
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
    def __init__(self) -> None:
        self._shells: dict[str, ManagedShell] = {}

    async def start(self, *, cmd: str, cwd: str | None) -> ManagedShell:
        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        shell = ManagedShell(shell_id=f"bash-{uuid.uuid4().hex[:8]}", cmd=cmd, cwd=cwd, process=process)
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
        if shell.returncode is not None:
            await self._finalize_shell(shell)
            return shell

        shell.process.terminate()
        try:
            async with asyncio.timeout(3):
                await shell.process.wait()
        except TimeoutError:
            shell.process.kill()
            await shell.process.wait()
        await self._finalize_shell(shell)
        return shell

    async def wait_closed(self, shell_id: str) -> ManagedShell:
        shell = self.get(shell_id)
        if shell.returncode is None:
            await shell.process.wait()
        await self._finalize_shell(shell)
        return shell

    async def _finalize_shell(self, shell: ManagedShell) -> None:
        for task in shell.read_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
