from __future__ import annotations

import ast
import asyncio
import inspect
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from pydantic import BaseModel, Field

from bub.builtin.codemode import (
    CODE_TOOLS_STATE_KEY,
    RUN_CODE_TOOL_NAME,
    render_tool_stub,
    run_code,
    set_code_mode,
    write_tool_stub,
)
from bub.builtin.environment import LocalEnvironment, LocalProcess
from bub.environment import ENVIRONMENT_STATE_KEY, CallTool, CodeFailed
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import ToolCall, ToolCallDecision, ToolCallResult
from bub.store import AsyncTapeStoreAdapter, InMemoryTapeStore
from bub.tape import Tape, TapeContext
from bub.tools import REGISTRY, Tool, ToolContext, ToolExecutor, model_tools, tool


class Order(TypedDict):
    """An order record."""

    id: str
    total: float


class Missing(TypedDict):
    error: str


class LookupInput(BaseModel):
    order_id: str = Field(..., description="The order identifier.")
    verbose: bool = Field(False, description="Include line items.")


def get_order(order_id: str, include_items: bool = False, *, context: ToolContext) -> Order | Missing:
    """Look up an order by id."""
    return {"id": order_id, "total": 12.5}


def _context(tmp_path: Path, tools: list[Tool] | None = None, **state: Any) -> ToolContext:
    tape = Tape(tmp_path, AsyncTapeStoreAdapter(InMemoryTapeStore()), TapeContext()).scoped("test-tape")
    if tools is not None:
        state[CODE_TOOLS_STATE_KEY] = model_tools(tools)
    return ToolContext(tape=tape, run_id="run-1", state=state)


def test_stub_declares_keyword_only_functions_with_types_and_docs() -> None:
    order_tool = Tool.from_callable(get_order, name="orders.get", context=True)

    stub = render_tool_stub([order_tool])

    ast.parse(stub)
    assert (
        "async def orders_get(*, order_id: str, include_items: bool = False) -> Order | Missing:\n"
        '    """Look up an order by id."""' in stub
    )
    assert 'class Order(TypedDict):\n    """An order record."""\n    id: str\n    total: float' in stub
    assert "class Missing(TypedDict):\n    error: str" in stub


def test_stub_documents_model_parameters_and_untyped_tools() -> None:
    def lookup(param: LookupInput) -> None:
        """Find things."""

    lookup_tool = tool(name="tests.codemode_lookup", model=LookupInput)(lookup)
    REGISTRY.pop(lookup_tool.name)

    untyped = Tool(
        name="mcp.search-docs",
        handler=lambda **_: None,
        description="Search docs.",
        parameters={"type": "object", "properties": {"from": {"type": "string"}}},
    )

    stub = render_tool_stub([lookup_tool, untyped])

    ast.parse(stub)
    assert "async def tests_codemode_lookup(*, order_id: str, verbose: bool = False) -> None:" in stub
    assert '    """Find things.' in stub
    assert "    order_id: The order identifier." in stub
    assert "async def mcp_search_docs(**kwargs: Any) -> Any:" in stub


def test_stub_path_is_stable_per_session_and_tool_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    order_tool = Tool.from_callable(get_order, name="orders.get", context=True)
    other_tool = Tool.from_callable(lambda: None, name="other")

    first = write_tool_stub([order_tool], session_id="user/1", workspace=tmp_path)
    again = write_tool_stub([order_tool], session_id="user/1", workspace=tmp_path)
    other_session = write_tool_stub([order_tool], session_id="user/2", workspace=tmp_path)
    other_tools = write_tool_stub([order_tool, other_tool], session_id="user/1", workspace=tmp_path)

    assert first == again
    assert first.is_relative_to(tmp_path / "home" / "codemode")
    assert first.read_text(encoding="utf-8") == render_tool_stub([order_tool])
    assert other_session.parent != first.parent
    assert other_tools.parent == first.parent
    assert other_tools != first


def test_run_code_is_a_preserved_tool() -> None:
    assert run_code.name == RUN_CODE_TOOL_NAME
    assert run_code.preserve is True
    assert run_code.parameters["required"] == ["code"]


@pytest.mark.asyncio
async def test_run_code_returns_printed_output_and_calls_tools_in_code_mode(tmp_path: Path) -> None:
    seen: list[bool] = []

    def get_mode(*, context: ToolContext) -> dict[str, bool]:
        seen.append(context.code_mode)
        return {"code_mode": context.code_mode}

    order_tool = Tool.from_callable(get_order, name="orders.get", context=True)
    mode_tool = Tool.from_callable(get_mode, name="mode", context=True)
    code = (
        "order = await tools.orders_get(order_id='A1')\n"
        "print(order['id'], order['total'])\n"
        "print(await tools.mode())\n"
        "def helper():\n"
        "    print('from helper')\n"
        "helper()\n"
    )

    output = await run_code.run(code=code, context=_context(tmp_path, [order_tool, mode_tool]))

    assert output == "A1 12.5\n{'code_mode': True}\nfrom helper\n"
    assert seen == [True]


@pytest.mark.asyncio
async def test_run_code_routes_tool_calls_through_hooks(tmp_path: Path) -> None:
    calls: list[tuple[str, bool]] = []

    class Hooks:
        async def before_tool_call(self, call: ToolCall, state: dict[str, Any]) -> tuple[ToolCall, ToolCallDecision]:
            calls.append((call.tool, call.code_mode))
            if call.tool == "blocked":
                return call, ToolCallDecision.deny("not allowed")
            return call, ToolCallDecision.proceed()

        async def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict[str, Any]) -> None:
            return None

    agent = type("FakeAgent", (), {"model_runner": type("Runner", (), {"hooks": Hooks()})()})()
    order_tool = Tool.from_callable(get_order, name="orders.get", context=True)
    blocked = Tool.from_callable(lambda: "secret", name="blocked")
    code = "try:\n    await tools.blocked()\nexcept Exception as exc:\n    print(type(exc).__name__, exc)\n"

    output = await run_code.run(code=code, context=_context(tmp_path, [order_tool, blocked], _runtime_agent=agent))

    assert output == "BubError [tool] not allowed\n"
    assert calls == [("blocked", True)]


@pytest.mark.asyncio
async def test_run_code_reports_exceptions_with_output_and_traceback(tmp_path: Path) -> None:
    code = "print('before')\nvalue = 1\nraise ValueError(f'bad {value}')\n"

    with pytest.raises(BubError) as exc_info:
        await run_code.run(code=code, context=_context(tmp_path, []))

    error = exc_info.value
    assert error.kind is ErrorKind.TOOL
    assert error.message == "Code raised ValueError: bad 1"
    assert error.details is not None
    assert error.details["output"] == "before\n"
    assert "line 3" in error.details["traceback"]
    assert "raise ValueError(f'bad {value}')" in error.details["traceback"]
    assert "codemode.py" not in error.details["traceback"]


@pytest.mark.asyncio
async def test_run_code_rejects_positional_tool_arguments(tmp_path: Path) -> None:
    order_tool = Tool.from_callable(get_order, name="orders.get", context=True)

    with pytest.raises(BubError, match="keyword arguments only"):
        await run_code.run(code="await tools.orders_get('A1')", context=_context(tmp_path, [order_tool]))


@pytest.mark.asyncio
async def test_run_code_requires_code_mode(tmp_path: Path) -> None:
    execution = await ToolExecutor().execute_async([(run_code, {"code": "print(1)"})], context=_context(tmp_path))

    assert execution.error is not None
    assert execution.error.message == "Code mode is not enabled for this run."


@pytest.mark.asyncio
async def test_code_mode_command_records_session_switch(tmp_path: Path) -> None:
    context = _context(tmp_path)

    result = await set_code_mode.run(enable=True, context=context)

    assert set_code_mode.name == "code_mode"
    assert set_code_mode.agent_use is False
    assert result == "Session code mode enabled (applies from the next turn)."
    assert context.state["code_mode"] is True
    entries = list(await context.tape.store.fetch_all(context.tape.query().kinds("event")))
    assert [entry.payload for entry in entries] == [{"name": "code_mode_switch", "data": {"code_mode": True}}]


@pytest.mark.asyncio
async def test_run_code_can_gather_tool_calls_concurrently(tmp_path: Path) -> None:
    import asyncio

    started: list[str] = []
    both_started = asyncio.Event()

    async def wait_for_peer(name: str) -> str:
        started.append(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return name

    wait_tool = Tool.from_callable(wait_for_peer, name="wait")
    code = "import asyncio\nprint(await asyncio.gather(tools.wait(name='a'), tools.wait(name='b')))\n"

    output = await run_code.run(code=code, context=_context(tmp_path, [wait_tool]))

    assert output == "['a', 'b']\n"


@pytest.mark.asyncio
async def test_run_code_without_await_executes_synchronously(tmp_path: Path) -> None:
    output = await run_code.run(code="print(sum(range(4)))", context=_context(tmp_path, []))

    assert output == "6\n"


class _RecordingEnvironment(LocalEnvironment):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.spawned: list[Sequence[str] | str] = []

    async def spawn(
        self, command: str | Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None
    ) -> LocalProcess:
        self.spawned.append(command)
        return await super().spawn(command, cwd=cwd, env=env)


@pytest.mark.asyncio
async def test_run_code_runs_in_a_process_of_the_session_environment(tmp_path: Path) -> None:
    environment = _RecordingEnvironment(tmp_path)
    context = _context(tmp_path, [], **{ENVIRONMENT_STATE_KEY: environment})

    output = await run_code.run(code="import os\nprint(os.getpid())", context=context)

    assert int(output) != os.getpid()
    assert len(environment.spawned) == 1
    assert environment.spawned[0][0] == environment.python


@pytest.mark.asyncio
async def test_run_code_uses_the_environment_workspace_as_working_directory(tmp_path: Path) -> None:
    context = _context(tmp_path, [], **{ENVIRONMENT_STATE_KEY: LocalEnvironment(tmp_path)})

    output = await run_code.run(code="import os\nprint(os.path.realpath(os.getcwd()))", context=context)

    assert output == f"{tmp_path.resolve()}\n"


@pytest.mark.asyncio
async def test_run_code_timeout_kills_the_process_and_keeps_printed_output(tmp_path: Path) -> None:
    code = "import time\nprint('started')\ntime.sleep(30)\n"

    with pytest.raises(BubError) as exc_info:
        await run_code.run(code=code, timeout_seconds=1, context=_context(tmp_path, []))

    assert exc_info.value.message == "Code timed out after 1 seconds"
    assert exc_info.value.details == {"output": "started\n"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_run_code_kills_processes_the_code_started(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid)\n"
    )

    output = await run_code.run(code=code, context=_context(tmp_path, []))

    pid = int(output)
    ps = shutil.which("ps")
    assert ps is not None
    for _ in range(100):
        status = subprocess.run([ps, "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
        if not status.stdout.strip() or status.stdout.strip().startswith("Z"):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"child process {pid} is still running")


@pytest.mark.asyncio
async def test_run_code_reports_a_runner_that_exits_without_a_result(tmp_path: Path) -> None:
    code = "import os, sys\nprint('partial', flush=True)\nsys.stderr.write('dying')\nsys.stderr.flush()\nos._exit(3)\n"

    with pytest.raises(BubError) as exc_info:
        await run_code.run(code=code, context=_context(tmp_path, []))

    assert exc_info.value.message == "Code runner exited unexpectedly with code 3"
    assert exc_info.value.details == {"output": "partial\n", "stderr": "dying"}


@pytest.mark.asyncio
async def test_run_code_passes_large_results_and_collects_all_stdout(tmp_path: Path) -> None:
    big_tool = Tool.from_callable(lambda size: {"text": "x" * size}, name="big")
    code = (
        "import sys\n"
        "result = await tools.big(size=200_000)\n"
        "sys.stdout.write(f'{len(result[\"text\"])}\\n')\n"
        "print('stray', file=sys.__stdout__, flush=True)\n"
    )

    output = await run_code.run(code=code, context=_context(tmp_path, [big_tool]))

    assert sorted(output.splitlines()) == ["200000", "stray"]


@pytest.mark.asyncio
async def test_run_code_raises_in_code_when_a_result_is_not_serializable(tmp_path: Path) -> None:
    opaque = Tool.from_callable(lambda: object(), name="opaque")
    code = "try:\n    await tools.opaque()\nexcept Exception as exc:\n    print(type(exc).__name__, 'not JSON serializable' in str(exc))\n"

    output = await run_code.run(code=code, context=_context(tmp_path, [opaque]))

    assert output == "BubError True\n"


class _InProcessEnvironment(LocalEnvironment):
    """Runs code on the event loop instead of spawning a process."""

    def __init__(self) -> None:
        super().__init__()
        self.stopped = False

    async def spawn(self, *args: Any, **kwargs: Any) -> LocalProcess:
        raise AssertionError("run_code must not spawn a process")

    async def run_code(
        self, code: str, *, tools: Sequence[str], call_tool: CallTool, write: Callable[[str], None]
    ) -> None:
        def bind(name: str) -> Callable[..., Any]:
            async def call(**kwargs: Any) -> Any:
                return await call_tool(name, kwargs)

            return call

        namespace = {
            "tools": SimpleNamespace(**{name: bind(name) for name in tools}),
            "print": lambda *args: write(" ".join(map(str, args)) + "\n"),
        }
        try:
            result = eval(compile(code, "<code>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), namespace)  # noqa: S307
            if inspect.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            self.stopped = True
            raise
        except BubError:
            raise
        except Exception as exc:
            raise CodeFailed(f"{type(exc).__name__}: {exc}") from exc


@pytest.mark.asyncio
async def test_run_code_delegates_to_the_environment_code_runtime(tmp_path: Path) -> None:
    calls: list[tuple[str, bool]] = []

    class Hooks:
        async def before_tool_call(self, call: ToolCall, state: dict[str, Any]) -> tuple[ToolCall, ToolCallDecision]:
            calls.append((call.tool, call.code_mode))
            if call.tool == "blocked":
                return call, ToolCallDecision.deny("not allowed")
            return call, ToolCallDecision.proceed()

        async def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict[str, Any]) -> None:
            return None

    agent = type("FakeAgent", (), {"model_runner": type("Runner", (), {"hooks": Hooks()})()})()
    order_tool = Tool.from_callable(get_order, name="orders.get", context=True)
    blocked = Tool.from_callable(lambda: "secret", name="blocked")
    code = (
        "order = await tools.orders_get(order_id='A1')\n"
        "print(order['id'], order['total'])\n"
        "try:\n    await tools.blocked()\nexcept Exception as exc:\n    print(exc)\n"
    )
    context = _context(
        tmp_path, [order_tool, blocked], _runtime_agent=agent, **{ENVIRONMENT_STATE_KEY: _InProcessEnvironment()}
    )

    output = await run_code.run(code=code, context=context)

    assert output == "A1 12.5\n[tool] not allowed\n"
    assert calls == [("orders_get", True), ("blocked", True)]


@pytest.mark.asyncio
async def test_run_code_timeout_cancels_the_environment_code_runtime(tmp_path: Path) -> None:
    environment = _InProcessEnvironment()
    context = _context(tmp_path, [], **{ENVIRONMENT_STATE_KEY: environment})

    with pytest.raises(BubError) as exc_info:
        await run_code.run(
            code="print('started')\nimport asyncio\nawait asyncio.sleep(30)", timeout_seconds=1, context=context
        )

    assert exc_info.value.message == "Code timed out after 1 seconds"
    assert exc_info.value.details == {"output": "started\n"}
    assert environment.stopped


@pytest.mark.asyncio
async def test_run_code_maps_code_and_runtime_failures_from_the_environment(tmp_path: Path) -> None:
    class FailingRuntime(_InProcessEnvironment):
        async def run_code(self, code: str, **kwargs: Any) -> None:
            kwargs["write"]("partial\n")
            raise BubError(ErrorKind.TOOL, "runtime crashed", details={"stderr": "boom"})

    context = _context(tmp_path, [], **{ENVIRONMENT_STATE_KEY: _InProcessEnvironment()})
    with pytest.raises(BubError) as code_error:
        await run_code.run(code="print('before')\nraise ValueError('bad')", context=context)
    assert code_error.value.message == "Code raised ValueError: bad"
    assert code_error.value.details == {"output": "before\n", "traceback": ""}

    context = _context(tmp_path, [], **{ENVIRONMENT_STATE_KEY: FailingRuntime()})
    with pytest.raises(BubError) as runtime_error:
        await run_code.run(code="print(1)", context=context)
    assert runtime_error.value.message == "runtime crashed"
    assert runtime_error.value.details == {"output": "partial\n", "stderr": "boom"}
