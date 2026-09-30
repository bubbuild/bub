from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import bub.builtin.tools as builtin_tools
from bub.builtin.shell_manager import ShellManager
from bub.framework import BubFramework
from bub.hooks import hookimpl
from bub.sandbox import SANDBOX_STATE_KEY, LocalProcess, LocalSandbox, Sandbox, sandbox_from_state
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.tape import Tape, TapeContext
from bub.tools import ToolContext


class MemorySandbox(Sandbox):
    """A sandbox whose files live in memory, at POSIX paths under /sandbox."""

    def __init__(self) -> None:
        self.workspace = "/sandbox/work"
        self.files: dict[str, str] = {}
        self.closed = False

    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> LocalProcess:
        raise NotImplementedError

    async def read_text(self, path: str) -> str:
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    async def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    async def aclose(self) -> None:
        self.closed = True


class RecordingSandbox(LocalSandbox):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.spawned: list[tuple[str | Sequence[str], str | None]] = []

    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> LocalProcess:
        self.spawned.append((command, cwd))
        return await super().spawn(command, cwd=cwd, env=env)


def _context(tmp_path: Path, sandbox: Sandbox) -> ToolContext:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    return ToolContext(tape=tape, run_id="run", state={"_runtime_workspace": str(tmp_path), SANDBOX_STATE_KEY: sandbox})


@pytest.mark.asyncio
async def test_fs_tools_read_and_write_through_the_session_sandbox(tmp_path: Path) -> None:
    sandbox = MemorySandbox()
    context = _context(tmp_path, sandbox)

    assert await builtin_tools.fs_write.run(path="notes/a.txt", content="one\ntwo", context=context) == (
        "wrote: /sandbox/work/notes/a.txt"
    )
    assert await builtin_tools.fs_edit.run(path="notes/a.txt", old="two", new="three", context=context) == (
        "edited: /sandbox/work/notes/a.txt"
    )
    assert await builtin_tools.fs_read.run(path="/sandbox/work/notes/a.txt", offset=1, context=context) == "three"
    assert sandbox.files == {"/sandbox/work/notes/a.txt": "one\nthree"}
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_bash_spawns_in_the_session_sandbox_with_cwd_resolved_there(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(builtin_tools, "shell_manager", ShellManager())
    (tmp_path / "sub").mkdir()
    sandbox = RecordingSandbox(tmp_path)
    context = _context(tmp_path, sandbox)

    assert await builtin_tools.bash.run(command="pwd", cwd="sub", context=context) == str(tmp_path / "sub")
    assert await builtin_tools.bash.run(command="pwd", context=context) == str(tmp_path)
    assert sandbox.spawned == [("pwd", str(tmp_path / "sub")), ("pwd", None)]


@pytest.mark.asyncio
async def test_bash_commands_get_eof_on_stdin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(builtin_tools, "shell_manager", ShellManager())
    context = _context(tmp_path, LocalSandbox(tmp_path))

    assert await builtin_tools.bash.run(command="cat; echo done", timeout_seconds=5, context=context) == "done"


def test_sandbox_from_state_falls_back_to_a_host_sandbox_for_the_workspace(tmp_path: Path) -> None:
    sandbox = sandbox_from_state({"_runtime_workspace": str(tmp_path)})

    assert isinstance(sandbox, LocalSandbox)
    assert sandbox.resolve_path("a/../b.txt") == str(tmp_path / "b.txt")
    with pytest.raises(ValueError, match="without a workspace"):
        sandbox_from_state({}).resolve_path("b.txt")


def test_default_path_resolution_uses_posix_paths_inside_the_sandbox() -> None:
    sandbox = MemorySandbox()

    assert sandbox.resolve_path("a/../b.txt") == "/sandbox/work/b.txt"
    assert sandbox.resolve_path("/etc/hosts") == "/etc/hosts"


@pytest.mark.asyncio
async def test_framework_caches_provided_sandbox_per_session_and_closes_it(tmp_path: Path) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    provided: list[tuple[str, Path, MemorySandbox]] = []

    class SandboxPlugin:
        @hookimpl
        def provide_sandbox(self, session_id: str, workspace: Path) -> Sandbox:
            sandbox = MemorySandbox()
            provided.append((session_id, workspace, sandbox))
            return sandbox

    framework.plugin_manager.register(SandboxPlugin(), name="sandbox")
    async with framework.running():
        first = await framework.build_state({"content": "hi"}, "session-a")
        again = await framework.build_state({"content": "hi"}, "session-a")
        other = await framework.build_state({"content": "hi"}, "session-b")

        assert first[SANDBOX_STATE_KEY] is again[SANDBOX_STATE_KEY] is provided[0][2]
        assert other[SANDBOX_STATE_KEY] is provided[1][2]
        assert [(session_id, workspace) for session_id, workspace, _ in provided] == [
            ("session-a", framework.workspace),
            ("session-b", framework.workspace),
        ]
        assert not any(sandbox.closed for _, _, sandbox in provided)
    assert all(sandbox.closed for _, _, sandbox in provided)


@pytest.mark.asyncio
async def test_framework_runs_tools_on_the_host_without_a_sandbox_provider(tmp_path: Path) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()

    state = await framework.build_state({"content": "hi"}, "session")

    sandbox = state[SANDBOX_STATE_KEY]
    assert isinstance(sandbox, LocalSandbox)
    assert sandbox.workspace == str(framework.workspace)
