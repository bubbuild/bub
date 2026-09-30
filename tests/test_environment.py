from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import bub.builtin.tools as builtin_tools
from bub.builtin.environment import LocalEnvironment, LocalProcess, environment_from_state
from bub.builtin.shell_manager import ShellManager
from bub.environment import ENVIRONMENT_STATE_KEY, Environment, Process
from bub.framework import BubFramework
from bub.hooks import hookimpl
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.tape import Tape, TapeContext
from bub.tools import ToolContext


class MemoryEnvironment(Environment):
    """An environment whose files live in memory, at POSIX paths under /environment."""

    def __init__(self) -> None:
        self.workspace = "/environment/work"
        self.files: dict[str, str] = {}
        self.closed = False

    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> Process:
        raise NotImplementedError

    async def read_text(self, path: str) -> str:
        try:
            return self.files[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    async def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    async def close(self) -> None:
        self.closed = True


class RecordingEnvironment(LocalEnvironment):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.spawned: list[tuple[str | Sequence[str], str | None]] = []

    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> LocalProcess:
        self.spawned.append((command, cwd))
        return await super().spawn(command, cwd=cwd, env=env)


def _context(tmp_path: Path, environment: Environment) -> ToolContext:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    return ToolContext(
        tape=tape, run_id="run", state={"_runtime_workspace": str(tmp_path), ENVIRONMENT_STATE_KEY: environment}
    )


@pytest.mark.asyncio
async def test_fs_tools_read_and_write_through_the_session_environment(tmp_path: Path) -> None:
    environment = MemoryEnvironment()
    context = _context(tmp_path, environment)

    assert await builtin_tools.fs_write.run(path="notes/a.txt", content="one\ntwo", context=context) == (
        "wrote: /environment/work/notes/a.txt"
    )
    assert await builtin_tools.fs_edit.run(path="notes/a.txt", old="two", new="three", context=context) == (
        "edited: /environment/work/notes/a.txt"
    )
    assert await builtin_tools.fs_read.run(path="/environment/work/notes/a.txt", offset=1, context=context) == "three"
    assert environment.files == {"/environment/work/notes/a.txt": "one\nthree"}
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_bash_spawns_in_the_session_environment_with_cwd_resolved_there(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(builtin_tools, "shell_manager", ShellManager())
    (tmp_path / "sub").mkdir()
    environment = RecordingEnvironment(tmp_path)
    context = _context(tmp_path, environment)

    assert await builtin_tools.bash.run(command="pwd", cwd="sub", context=context) == str(tmp_path / "sub")
    assert await builtin_tools.bash.run(command="pwd", context=context) == str(tmp_path)
    assert environment.spawned == [("pwd", str(tmp_path / "sub")), ("pwd", None)]


@pytest.mark.asyncio
async def test_bash_commands_get_eof_on_stdin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(builtin_tools, "shell_manager", ShellManager())
    context = _context(tmp_path, LocalEnvironment(tmp_path))

    assert await builtin_tools.bash.run(command="cat; echo done", timeout_seconds=5, context=context) == "done"


def test_environment_from_state_falls_back_to_a_host_environment_for_the_workspace(tmp_path: Path) -> None:
    environment = environment_from_state({"_runtime_workspace": str(tmp_path)})

    assert isinstance(environment, LocalEnvironment)
    assert environment.resolve_path("a/../b.txt") == str(tmp_path / "b.txt")
    with pytest.raises(ValueError, match="without a workspace"):
        environment_from_state({}).resolve_path("b.txt")


def test_default_path_resolution_uses_posix_paths_inside_the_environment() -> None:
    environment = MemoryEnvironment()

    assert environment.resolve_path("a/../b.txt") == "/environment/work/b.txt"
    assert environment.resolve_path("/etc/hosts") == "/etc/hosts"


@pytest.mark.asyncio
async def test_framework_caches_provided_environment_per_session_and_closes_it(tmp_path: Path) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    provided: list[tuple[str, Path, MemoryEnvironment]] = []

    class EnvironmentPlugin:
        @hookimpl
        def provide_environment(self, session_id: str, workspace: Path) -> Environment:
            environment = MemoryEnvironment()
            provided.append((session_id, workspace, environment))
            return environment

    framework.plugin_manager.register(EnvironmentPlugin(), name="environment")
    async with framework.running():
        first = await framework.build_state({"content": "hi"}, "session-a")
        again = await framework.build_state({"content": "hi"}, "session-a")
        other = await framework.build_state({"content": "hi"}, "session-b")

        assert first[ENVIRONMENT_STATE_KEY] is again[ENVIRONMENT_STATE_KEY] is provided[0][2]
        assert other[ENVIRONMENT_STATE_KEY] is provided[1][2]
        assert [(session_id, workspace) for session_id, workspace, _ in provided] == [
            ("session-a", framework.workspace),
            ("session-b", framework.workspace),
        ]
        assert not any(environment.closed for _, _, environment in provided)
    assert all(environment.closed for _, _, environment in provided)


@pytest.mark.asyncio
async def test_builtin_hooks_provide_a_host_environment_for_the_workspace(tmp_path: Path) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()

    state = await framework.build_state({"content": "hi"}, "session")

    environment = state[ENVIRONMENT_STATE_KEY]
    assert isinstance(environment, LocalEnvironment)
    assert environment.workspace == str(framework.workspace)


@pytest.mark.asyncio
async def test_framework_has_no_environment_without_a_provider(tmp_path: Path) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")

    state = await framework.build_state({"content": "hi"}, "session")

    assert await framework.get_environment("session") is None
    assert ENVIRONMENT_STATE_KEY not in state
