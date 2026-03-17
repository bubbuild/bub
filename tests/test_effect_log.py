"""End-to-end tests for effect-log integration with bub's builtin tools.

Demonstrates that when effect-log is enabled:
1. bash (IrreversibleWrite) is NOT re-executed on recovery
2. fs.read (ReadOnly) IS replayed for fresh data
3. fs.write (IdempotentWrite) returns sealed result on recovery
"""

from __future__ import annotations

from pathlib import Path

import pytest
from republic import ToolContext

from bub.builtin.tools import bash, fs_read, fs_write
from bub.tools import EFFECT_KINDS, REGISTRY, enable_effect_log, make_adapted_handler, unwrap_handler


def _tool_context(workspace: Path) -> ToolContext:
    return ToolContext(tape="test-tape", run_id="test-run", state={"_runtime_workspace": str(workspace)})


def _make_tooldefs(tool_names: list[str]):
    """Build EffectLog ToolDef entries for the given builtin tools."""
    from effect_log import EffectKind, ToolDef

    defs = []
    for name in tool_names:
        if name not in EFFECT_KINDS or name not in REGISTRY:
            continue
        tool_instance = REGISTRY[name]
        handler = tool_instance.handler
        if handler is None:
            continue
        raw = unwrap_handler(handler)
        kind = getattr(EffectKind, EFFECT_KINDS[name])
        defs.append(ToolDef(name, kind, make_adapted_handler(raw, tool_instance.context)))
    return defs


@pytest.fixture
def workspace(tmp_path):
    """Create a workspace with a test file."""
    config = tmp_path / "config.toml"
    config.write_text('[deploy]\nregion = "us-east-1"\n')
    return tmp_path


@pytest.fixture
def effect_db(tmp_path):
    """Return a fresh sqlite storage URI."""
    return f"sqlite:///{tmp_path / 'effects.db'}"


@pytest.mark.asyncio
async def test_e2e_builtin_tools_crash_recovery(workspace, effect_db):
    """Full cycle with real builtin tools: execute → crash → recover.

    Scenario: an agent reads a config, runs a deploy command, then writes
    a status file. After a crash, the deploy command must NOT re-execute.
    """
    from effect_log import EffectLog

    tool_names = ["fs.read", "bash", "fs.write"]
    ctx = _tool_context(workspace)

    # Track real calls via wrapper
    call_counts = {"fs.read": 0, "bash": 0, "fs.write": 0}
    original_handlers = {name: REGISTRY[name].handler for name in tool_names}

    # -- Phase 1: Normal execution -------------------------------------------

    tooldefs = _make_tooldefs(tool_names)
    log = EffectLog(execution_id="deploy-001", tools=tooldefs, storage=effect_db)
    enable_effect_log(log)

    # 1. Read config (ReadOnly)
    r1 = await REGISTRY["fs.read"].run(path=str(workspace / "config.toml"), context=ctx)
    assert "us-east-1" in r1

    # 2. Deploy via bash (IrreversibleWrite)
    r2 = await REGISTRY["bash"].run(cmd="echo deployed-to-prod", context=ctx)
    assert "deployed-to-prod" in r2

    # 3. Write status (IdempotentWrite)
    r3 = await REGISTRY["fs.write"].run(
        path=str(workspace / "status.txt"), content="deployed", context=ctx
    )
    assert "wrote:" in r3

    # Verify history
    history = log.history()
    assert len(history) == 3
    assert history[0]["effect_kind"] == "ReadOnly"
    assert history[1]["effect_kind"] == "IrreversibleWrite"
    assert history[2]["effect_kind"] == "IdempotentWrite"

    # -- Phase 2: Crash recovery ---------------------------------------------

    # Restore original handlers to re-wrap (simulates new process)
    from dataclasses import replace as dc_replace

    for name, handler in original_handlers.items():
        REGISTRY[name] = dc_replace(REGISTRY[name], handler=handler)

    # Get raw (unwrapped) handlers for counting wrappers to call
    original_raw = {name: unwrap_handler(REGISTRY[name].handler) for name in tool_names}

    # Install counting handlers BEFORE building tooldefs so adapted captures them
    async def counting_bash(*args, **kwargs):
        call_counts["bash"] += 1
        return await original_raw["bash"](*args, **kwargs)

    def counting_fs_read(*args, **kwargs):
        call_counts["fs.read"] += 1
        return original_raw["fs.read"](*args, **kwargs)

    def counting_fs_write(*args, **kwargs):
        call_counts["fs.write"] += 1
        return original_raw["fs.write"](*args, **kwargs)

    REGISTRY["bash"] = dc_replace(REGISTRY["bash"], handler=counting_bash)
    REGISTRY["fs.read"] = dc_replace(REGISTRY["fs.read"], handler=counting_fs_read)
    REGISTRY["fs.write"] = dc_replace(REGISTRY["fs.write"], handler=counting_fs_write)

    tooldefs2 = _make_tooldefs(tool_names)
    log2 = EffectLog(execution_id="deploy-001", tools=tooldefs2, storage=effect_db, recover=True)
    enable_effect_log(log2)

    # Re-execute the same sequence
    r1 = await REGISTRY["fs.read"].run(path=str(workspace / "config.toml"), context=ctx)
    r2 = await REGISTRY["bash"].run(cmd="echo deployed-to-prod", context=ctx)
    r3 = await REGISTRY["fs.write"].run(
        path=str(workspace / "status.txt"), content="deployed", context=ctx
    )

    # Results are identical (sealed or replayed)
    assert "us-east-1" in r1
    assert "deployed-to-prod" in r2
    assert "wrote:" in r3

    # THE KEY: bash was NOT re-executed
    assert call_counts["fs.read"] == 1, "ReadOnly: replayed for fresh data"
    assert call_counts["bash"] == 0, "IrreversibleWrite: sealed, NOT re-executed"
    assert call_counts["fs.write"] == 0, "IdempotentWrite: sealed, NOT re-executed"


@pytest.mark.asyncio
async def test_builtin_tools_have_effect_kinds():
    """All builtin tools should declare their effect kind."""
    expected = {
        "bash": "IrreversibleWrite",
        "bash.output": "ReadOnly",
        "bash.kill": "IrreversibleWrite",
        "fs.read": "ReadOnly",
        "fs.write": "IdempotentWrite",
        "fs.edit": "IdempotentWrite",
        "skill": "ReadOnly",
        "tape.info": "ReadOnly",
        "tape.search": "ReadOnly",
        "tape.reset": "IrreversibleWrite",
        "tape.handoff": "IdempotentWrite",
        "tape.anchors": "ReadOnly",
        "web.fetch": "ReadOnly",
        "subagent": "IrreversibleWrite",
        "help": "ReadOnly",
    }
    for name, kind in expected.items():
        assert name in EFFECT_KINDS, f"{name} should have an effect kind"
        assert EFFECT_KINDS[name] == kind, f"{name}: expected {kind}, got {EFFECT_KINDS[name]}"
