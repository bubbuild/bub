from __future__ import annotations

import asyncio
import contextlib
import shlex
import sys
from types import SimpleNamespace

import pytest

import bub.builtin.tools as builtin_tools
from bub.builtin.shell_manager import ShellManager
from bub.builtin.tape import Tape
from bub.builtin.tools import (
    bash,
    bash_output,
    completion_tools,
    kill_bash,
    model_tools,
    quit_tool,
    render_tools_prompt,
    resolve_tool_names,
)
from bub.runtime import ErrorKind
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeContext
from bub.tools import REGISTRY, Tool, ToolContext, ToolExecutor, tool


def _tool_context(tmp_path, **state) -> ToolContext:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    return ToolContext(tape=tape, run_id="test-run", state={"_runtime_workspace": str(tmp_path), **state})


def _python_shell(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_completion_tools_builds_any_llm_payload() -> None:
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    sample_tool = Tool(
        name="tests_sample_tool",
        description="Sample tool",
        parameters=parameters,
        handler=lambda value: value,
    )

    assert completion_tools([sample_tool]) == [
        {
            "type": "function",
            "function": {
                "name": "tests_sample_tool",
                "description": "Sample tool",
                "parameters": parameters,
            },
        }
    ]


def test_model_tools_rewrites_dotted_names_without_mutating_original() -> None:
    tool_name = "tests.rename_me"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="rename")
    def rename_me(value: str) -> str:
        return "ok"

    rewritten = model_tools([rename_me])

    assert [item.name for item in rewritten] == ["tests_rename_me"]
    assert rewritten[0].parameters == rename_me.parameters
    assert rename_me.name == tool_name
    assert "additionalProperties" not in rename_me.parameters


def test_render_tools_prompt_renders_available_tools_block() -> None:
    first_name = "tests.prompt_one"
    second_name = "tests.prompt_two"
    REGISTRY.pop(first_name, None)
    REGISTRY.pop(second_name, None)

    @tool(name=first_name, description="First tool")
    def prompt_one() -> str:
        return "one"

    @tool(name=second_name)
    def prompt_two() -> str:
        return "two"

    rendered = render_tools_prompt([prompt_one, prompt_two])

    assert rendered == "<available_tools>\n- tests_prompt_one(): First tool\n- tests_prompt_two()\n</available_tools>"


def test_render_tools_prompt_includes_model_name_and_parameter_signature() -> None:
    tool_name = "tests.prompt_signature"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="Read a file")
    def prompt_signature(path: str, offset: int = 0) -> str:
        return f"{path}:{offset}"

    rendered = render_tools_prompt([prompt_signature])

    assert rendered == "<available_tools>\n- tests_prompt_signature(path, offset?): Read a file\n</available_tools>"


def test_render_tools_prompt_returns_empty_string_for_empty_input() -> None:
    assert render_tools_prompt([]) == ""


def test_resolve_tool_names_accepts_runtime_names_and_model_aliases() -> None:
    dotted_name = "tests.resolve_alias"
    underscored_name = "tests_with_underscore"
    excluded_name = "tests.excluded_tool"
    REGISTRY.pop(dotted_name, None)
    REGISTRY.pop(underscored_name, None)
    REGISTRY.pop(excluded_name, None)

    @tool(name=dotted_name)
    def resolve_alias() -> str:
        return "alias"

    @tool(name=underscored_name)
    def resolve_runtime_name() -> str:
        return "runtime"

    @tool(name=excluded_name)
    def excluded_tool() -> str:
        return "excluded"

    assert resolve_tool_names(
        [" tests_resolve_alias ", " tests_with_underscore "], exclude={" tests_excluded_tool "}
    ) == {
        dotted_name,
        underscored_name,
    }
    assert dotted_name not in resolve_tool_names(None, exclude={" tests_resolve_alias "})
    assert excluded_name not in resolve_tool_names(None, exclude={" tests_excluded_tool "})
    assert resolve_tool_names(None, exclude={" tests_resolve_alias "}) >= {underscored_name}


def test_resolve_tool_names_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="tests_missing_tool"):
        resolve_tool_names([" tests_missing_tool "])

    with pytest.raises(ValueError, match="tests_missing_tool"):
        resolve_tool_names(None, exclude={" tests_missing_tool "})


@pytest.mark.asyncio
async def test_bash_returns_stdout_for_foreground_command(tmp_path) -> None:
    result = await bash.run(cmd=_python_shell("print('hello')"), context=_tool_context(tmp_path))

    assert result == "hello"


@pytest.mark.asyncio
async def test_foreground_bash_releases_shell_from_shell_manager(tmp_path, monkeypatch) -> None:
    manager = ShellManager()
    monkeypatch.setattr(builtin_tools, "shell_manager", manager)

    result = await bash.run(cmd=_python_shell("print('hello')"), context=_tool_context(tmp_path))

    assert result == "hello"
    assert manager._shells == {}


@pytest.mark.asyncio
async def test_foreground_bash_releases_shell_when_command_fails(tmp_path, monkeypatch) -> None:
    manager = ShellManager()
    monkeypatch.setattr(builtin_tools, "shell_manager", manager)

    with pytest.raises(RuntimeError, match="command exited with code"):
        await bash.run(cmd=_python_shell("import sys; sys.exit(2)"), context=_tool_context(tmp_path))

    assert manager._shells == {}


@pytest.mark.asyncio
async def test_foreground_bash_terminates_shell_when_cancelled(tmp_path, monkeypatch) -> None:
    manager = ShellManager()
    monkeypatch.setattr(builtin_tools, "shell_manager", manager)

    task = asyncio.create_task(
        bash.run(
            cmd=_python_shell("import time; time.sleep(10)"),
            context=_tool_context(tmp_path, session_id="session:target"),
        )
    )
    await asyncio.sleep(0.1)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert manager._shells == {}


@pytest.mark.asyncio
async def test_bash_non_zero_exit_is_returned_as_tool_error(tmp_path) -> None:
    command = _python_shell("import sys; print('boom'); sys.exit(7)")
    executor = ToolExecutor()

    result = await executor.execute_async([(bash, {"cmd": command})], context=_tool_context(tmp_path))

    assert result.error is not None
    assert result.error.kind is ErrorKind.TOOL
    assert len(result.tool_results) == 1
    tool_result = result.tool_results[0]
    assert tool_result["kind"] == "tool"
    assert tool_result["message"] == "Tool 'bash' execution failed."
    error_detail = tool_result["details"]["error"]
    assert "command exited with code 7" in error_detail
    assert "boom" in error_detail


@pytest.mark.asyncio
async def test_background_bash_exposes_output_via_bash_output(tmp_path) -> None:
    command = _python_shell(
        "import sys, time; print('start'); sys.stdout.flush(); time.sleep(0.2); print('done'); sys.stdout.flush()"
    )

    started = await bash.run(cmd=command, background=True, context=_tool_context(tmp_path))
    shell_id = started.removeprefix("started: ").strip()

    await asyncio.sleep(0.35)
    output = await bash_output.run(shell_id=shell_id)

    assert output.startswith(f"id: {shell_id}\nstatus: exited\n")
    assert "exit_code: 0" in output
    assert "start" in output
    assert "done" in output


@pytest.mark.asyncio
async def test_kill_bash_terminates_background_process_and_releases_shell(tmp_path) -> None:
    started = await bash.run(
        cmd=_python_shell("import time; time.sleep(10)"),
        background=True,
        context=_tool_context(tmp_path),
    )
    shell_id = started.removeprefix("started: ").strip()

    killed = await kill_bash.run(shell_id=shell_id)

    assert killed.startswith(f"id: {shell_id}\nstatus: exited\nexit_code: ")
    assert "exit_code: null" not in killed
    with pytest.raises(KeyError, match="unknown shell id"):
        await bash_output.run(shell_id=shell_id)


@pytest.mark.asyncio
async def test_kill_bash_returns_status_when_process_already_finished(tmp_path) -> None:
    started = await bash.run(
        cmd=_python_shell("print('done')"),
        background=True,
        context=_tool_context(tmp_path),
    )
    shell_id = started.removeprefix("started: ").strip()

    await asyncio.sleep(0.1)
    result = await kill_bash.run(shell_id=shell_id)

    assert result == f"id: {shell_id}\nstatus: exited\nexit_code: 0"


@pytest.mark.asyncio
async def test_quit_tool_terminates_background_shells_for_current_session(tmp_path, monkeypatch) -> None:
    manager = ShellManager()
    monkeypatch.setattr(builtin_tools, "shell_manager", manager)

    target_started = await bash.run(
        cmd=_python_shell("import time; time.sleep(10)"),
        background=True,
        context=_tool_context(tmp_path, session_id="session:target"),
    )
    target_shell_id = target_started.removeprefix("started: ").strip()
    other_started = await bash.run(
        cmd=_python_shell("import time; time.sleep(10)"),
        background=True,
        context=_tool_context(tmp_path, session_id="session:other"),
    )
    other_shell_id = other_started.removeprefix("started: ").strip()

    class FakeFramework:
        def __init__(self) -> None:
            self.quit_sessions: list[str] = []

        async def quit_via_router(self, session_id: str) -> None:
            self.quit_sessions.append(session_id)

    framework = FakeFramework()
    context = _tool_context(
        tmp_path,
        session_id="session:target",
        _runtime_agent=SimpleNamespace(framework=framework),
    )

    result = await quit_tool.run(context=context)

    assert result == "Session tasks stopped."
    assert framework.quit_sessions == ["session:target"]
    with pytest.raises(KeyError, match="unknown shell id"):
        await bash_output.run(shell_id=target_shell_id)
    assert manager.get(other_shell_id).returncode is None

    await kill_bash.run(shell_id=other_shell_id)
