from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import bub.builtin.shell_manager as shell_module
from bub.builtin.shell_manager import ManagedShell, ShellManager
from bub.framework import BubFramework


def _python(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


async def _sleeping_shell(manager: ShellManager, *, ignore_term: bool = False) -> ManagedShell:
    code = "import signal, time; "
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    code += "print('ready', flush=True); time.sleep(30)"
    shell = await manager.start(cmd=("exec " if os.name != "nt" else "") + _python(code), cwd=None, session_id="same")
    async with asyncio.timeout(5):
        while "ready" not in shell.output:
            await asyncio.sleep(0.01)
    return shell


def _framework(tmp_path: Path) -> BubFramework:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    return framework


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ["normal", "error", "cancel"])
async def test_framework_lifespan_terminates_background_shells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_kind: str
) -> None:
    manager = ShellManager()
    monkeypatch.setattr(shell_module, "shell_manager", manager)
    framework = _framework(tmp_path)
    ready = asyncio.Event()
    finish = asyncio.Event()
    shells: list[ManagedShell] = []

    async def run() -> None:
        async with framework.running():
            shells.append(await _sleeping_shell(manager))
            ready.set()
            await finish.wait()
            if exit_kind == "error":
                raise RuntimeError("runtime failed")

    task = asyncio.create_task(run())
    try:
        async with asyncio.timeout(5):
            await ready.wait()
        if exit_kind == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif exit_kind == "error":
            finish.set()
            with pytest.raises(RuntimeError, match="runtime failed"):
                await task
        else:
            finish.set()
            await task
        assert shells[0].returncode is not None
        assert all(reader.done() for reader in shells[0].read_tasks)
        assert manager._shells == {}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for shell_id in list(manager._shells):
            await manager.terminate(shell_id)


@pytest.mark.asyncio
async def test_lifespan_and_session_cleanup_do_not_kill_another_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ShellManager()
    monkeypatch.setattr(shell_module, "shell_manager", manager)
    async with _framework(tmp_path).running():
        outer = await _sleeping_shell(manager)
        async with _framework(tmp_path).running():
            inner = await _sleeping_shell(manager)
            assert await manager.terminate_session("same") == 1
            assert inner.returncode is not None
            assert outer.returncode is None
            inner = await _sleeping_shell(manager)
        assert inner.returncode is not None
        assert outer.returncode is None
    assert outer.returncode is not None
    assert manager._shells == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_wait_closed_terminates_redirected_children_after_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ShellManager()
    monkeypatch.setattr(manager, "TERMINATE_TIMEOUT", 0.1)
    pid_file = tmp_path / "child.pid"
    child = _python(
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    shell = await manager.start(cmd=f"{child} >/dev/null 2>&1 &", cwd=None)
    try:
        async with asyncio.timeout(5):
            while not pid_file.exists():
                await asyncio.sleep(0.01)
            await manager.wait_closed(shell.shell_id)
        pid = int(pid_file.read_text())
        ps = shutil.which("ps")
        assert ps is not None
        status = subprocess.run([ps, "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
        assert not status.stdout.strip() or status.stdout.strip().startswith("Z")
        assert manager._shells == {}
    finally:
        manager._signal_shell(shell, kill=True)
        if shell.shell_id in manager._shells:
            await manager.terminate(shell.shell_id)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["terminate", "session", "lifespan"])
async def test_repeated_cancellation_waits_for_forced_termination(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    manager = ShellManager()
    monkeypatch.setattr(manager, "TERMINATE_TIMEOUT", 0.2)
    terminating = asyncio.Event()
    signalled = manager._signal_shell
    shells: list[ManagedShell] = []

    def signal_shell(shell: ManagedShell, *, kill: bool) -> None:
        signalled(shell, kill=kill)
        if not kill:
            terminating.set()

    monkeypatch.setattr(manager, "_signal_shell", signal_shell)

    async def cleanup() -> None:
        async with manager.lifespan():
            shell = await _sleeping_shell(manager, ignore_term=True)
            shells.append(shell)
            if operation == "terminate":
                await manager.terminate(shell.shell_id)
            elif operation == "session":
                await manager.terminate_session("same")

    task = asyncio.create_task(cleanup())
    try:
        async with asyncio.timeout(5):
            await terminating.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert shells[0].returncode == -signal.SIGKILL
        assert manager._shells == {}
    finally:
        for shell in shells:
            signalled(shell, kill=True)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_during_spawn_waits_for_process_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ShellManager()
    spawned = asyncio.Event()
    finish_spawn = asyncio.Event()
    shells: list[ManagedShell] = []
    start = manager._start

    async def delayed_start(**kwargs) -> ManagedShell:
        shell = await start(**kwargs)
        shells.append(shell)
        spawned.set()
        await finish_spawn.wait()
        return shell

    monkeypatch.setattr(manager, "_start", delayed_start)
    task = asyncio.create_task(manager.start(cmd=_python("import time; time.sleep(30)"), cwd=None))
    try:
        async with asyncio.timeout(5):
            await spawned.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        finish_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert shells[0].returncode is not None
        assert manager._shells == {}
        assert manager._starting == {}
    finally:
        finish_spawn.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for shell_id in list(manager._shells):
            await manager.terminate(shell_id)


@pytest.mark.asyncio
async def test_lifespan_waits_for_inflight_spawn_before_finishing(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ShellManager()
    spawning = asyncio.Event()
    finish_spawn = asyncio.Event()
    shells: list[ManagedShell] = []
    start_tasks: list[asyncio.Task[ManagedShell]] = []
    start = manager._start

    async def delayed_start(**kwargs) -> ManagedShell:
        spawning.set()
        await finish_spawn.wait()
        shell = await start(**kwargs)
        shells.append(shell)
        return shell

    monkeypatch.setattr(manager, "_start", delayed_start)

    async def run() -> None:
        async with manager.lifespan():
            start_tasks.append(asyncio.create_task(manager.start(cmd=_python("import time; time.sleep(30)"), cwd=None)))
            await spawning.wait()

    task = asyncio.create_task(run())
    try:
        async with asyncio.timeout(5):
            await spawning.wait()
        await asyncio.sleep(0)
        assert not task.done()
        finish_spawn.set()
        await task
        with pytest.raises(RuntimeError, match="shell runtime is closed"):
            await start_tasks[0]
        assert shells[0].returncode is not None
        assert manager._shells == {}
        assert manager._starting == {}
    finally:
        finish_spawn.set()
        await asyncio.gather(task, *start_tasks, return_exceptions=True)
        for shell_id in list(manager._shells):
            await manager.terminate(shell_id)


@pytest.mark.asyncio
async def test_task_cannot_start_shell_after_its_runtime_has_closed() -> None:
    manager = ShellManager()
    finish = asyncio.Event()

    async def late_start() -> ManagedShell:
        await finish.wait()
        return await manager.start(cmd=_python("pass"), cwd=None)

    async with manager.lifespan():
        task = asyncio.create_task(late_start())
    finish.set()
    with pytest.raises(RuntimeError, match="shell runtime is closed"):
        await task
    assert manager._shells == {}


@pytest.mark.asyncio
async def test_startup_failure_cleans_shells_from_entered_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bub.hooks import hookimpl

    manager = ShellManager()
    monkeypatch.setattr(shell_module, "shell_manager", manager)
    framework = _framework(tmp_path)
    shells: list[ManagedShell] = []

    class FailingStore:
        @hookimpl
        async def provide_tape_store(self):
            shells.append(await _sleeping_shell(manager))
            raise RuntimeError("store setup failed")
            yield  # pragma: no cover

    framework.plugin_manager.register(FailingStore(), name="failing-store")
    with pytest.raises(RuntimeError, match="store setup failed"):
        async with framework.running():
            pytest.fail("startup must fail")
    assert shells[0].returncode is not None
    assert manager._shells == {}


@pytest.mark.asyncio
async def test_lifespan_cleans_remaining_shells_after_a_reader_fails() -> None:
    manager = ShellManager()
    shells: list[ManagedShell] = []

    async def broken_reader() -> None:
        raise OSError("output pipe failed")

    with pytest.raises(ExceptionGroup, match="shell cleanup failed"):
        async with manager.lifespan():
            shells.extend([await _sleeping_shell(manager), await _sleeping_shell(manager)])
            reader = asyncio.create_task(broken_reader())
            shells[0].read_tasks.append(reader)
            await asyncio.gather(reader, return_exceptions=True)
    assert all(shell.returncode is not None for shell in shells)
    assert manager._shells == {}
