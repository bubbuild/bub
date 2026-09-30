from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Any, TypedDict

import pytest
from pydantic import BaseModel, Field

from bub.builtin.codemode import (
    CODE_TOOLS_STATE_KEY,
    RUN_CODE_TOOL_NAME,
    render_tool_stub,
    run_code,
    set_code_mode,
)
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

    order_tool = Tool.from_callable(get_order, name="orders.get", context=True)
    blocked = Tool.from_callable(lambda: "secret", name="blocked")
    code = "try:\n    await tools.blocked()\nexcept Exception as exc:\n    print(type(exc).__name__, exc)\n"

    output = await run_code.run(code=code, context=replace(_context(tmp_path, [order_tool, blocked]), hooks=Hooks()))

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


@pytest.mark.asyncio
async def test_code_callback_uses_same_environment_as_direct_tool(tmp_path: Path) -> None:
    from bub.builtin.environment import LocalExecutionEnvironment
    from bub.builtin.tools import fs_read

    class Environment(LocalExecutionEnvironment):
        async def read_file(self, path: str) -> str:
            return "environment resource"

    context = _context(tmp_path, [fs_read], _runtime_execution_environment=Environment(tmp_path))
    assert await fs_read.run(path="remote", context=context) == "environment resource"
    assert (
        await run_code.run(code="print(await tools.fs_read(path='remote'))", context=context)
        == "environment resource\n"
    )


@pytest.mark.asyncio
async def test_adapter_callbacks_keep_host_context_hooks_and_allowed_tools(tmp_path: Path) -> None:
    from bub.builtin.codemode import code_tool_callbacks

    seen: list[tuple[str, bool]] = []
    binding = object()

    async def host(*, context: ToolContext) -> dict[str, bool]:
        return {"pinned": context.state["_runtime_execution_environment"] is binding, "code": context.code_mode}

    class Hooks:
        async def before_tool_call(self, call: ToolCall, state: dict[str, Any]) -> tuple[ToolCall, ToolCallDecision]:
            seen.append((call.tool, call.code_mode))
            if call.tool == "blocked":
                return call, ToolCallDecision.deny("host policy")
            return call, ToolCallDecision.proceed()

        async def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: dict[str, Any]) -> None:
            seen.append(("after:" + call.tool, call.code_mode))

    context = replace(
        _context(
            tmp_path,
            [Tool.from_callable(host, name="host", context=True), Tool("blocked", lambda: "secret")],
            _runtime_execution_environment=binding,
        ),
        hooks=Hooks(),
    )
    callbacks = code_tool_callbacks(context)
    assert await callbacks["host"]() == {"pinned": True, "code": True}
    with pytest.raises(BubError, match="host policy"):
        await callbacks["blocked"]()
    with pytest.raises(KeyError):
        callbacks["bash"]
    assert seen == [("host", True), ("after:host", True), ("blocked", True), ("after:blocked", True)]
    assert context.code_mode is False


def test_adapter_callbacks_require_enabled_code_mode(tmp_path: Path) -> None:
    from bub.builtin.codemode import code_tool_callbacks

    with pytest.raises(BubError, match="Code mode is not enabled"):
        code_tool_callbacks(_context(tmp_path))


@pytest.mark.asyncio
async def test_code_only_environment_dispatches_without_local_execution(tmp_path: Path) -> None:
    from bub.builtin.environment import available_turn_tools
    from bub.builtin.tools import bash, fs_read

    class RemoteCode:
        async def execute_code(self, code, callbacks):
            assert code == "opaque remote program"
            assert set(callbacks) == {"host"}
            return await callbacks["host"]()

    async def host() -> str:
        return "remote callback result"

    environment = RemoteCode()
    host_tool = Tool.from_callable(host)
    registry = {t.name: t for t in (run_code, bash, fs_read, host_tool)}
    available = available_turn_tools(registry, environment)
    assert set(available) == {"run_code", "host"}
    assert available["run_code"] is run_code
    context = _context(tmp_path, [host_tool], _runtime_execution_environment=environment)
    assert await run_code.run(code="opaque remote program", context=context) == "remote callback result"
