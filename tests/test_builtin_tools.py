from __future__ import annotations

import asyncio
import shlex
import sys

import pytest
from republic import ToolContext
from republic.core.errors import ErrorKind
from republic.tools.executor import ToolExecutor

from bub.builtin.tools import bash, bash_output, kill_bash


def _tool_context(tmp_path) -> ToolContext:
    return ToolContext(tape="test-tape", run_id="test-run", state={"_runtime_workspace": str(tmp_path)})


def _python_shell(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.mark.asyncio
async def test_bash_returns_stdout_for_foreground_command(tmp_path) -> None:
    result = await bash.run(cmd=_python_shell("print('hello')"), context=_tool_context(tmp_path))

    assert result == "hello"


@pytest.mark.asyncio
async def test_bash_non_zero_exit_is_returned_as_tool_error(tmp_path) -> None:
    command = _python_shell("import sys; print('boom'); sys.exit(7)")
    executor = ToolExecutor()
    tool_call = {
        "type": "function",
        "function": {
            "name": bash.name,
            "arguments": {"cmd": command},
        },
    }

    result = await executor.execute_async([tool_call], tools=[bash], context=_tool_context(tmp_path))

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
async def test_kill_bash_terminates_background_process(tmp_path) -> None:
    started = await bash.run(
        cmd=_python_shell("import time; time.sleep(10)"),
        background=True,
        context=_tool_context(tmp_path),
    )
    shell_id = started.removeprefix("started: ").strip()

    killed = await kill_bash.run(shell_id=shell_id)
    output = await bash_output.run(shell_id=shell_id)

    assert killed.startswith(f"id: {shell_id}\nstatus: exited\nexit_code: ")
    assert "exit_code: null" not in killed
    assert output.startswith(f"id: {shell_id}\nstatus: exited\n")


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
