import pytest

from bub.builtin.environment import LocalExecutionEnvironment
from bub.framework import BubFramework
from bub.hooks import hookimpl


@pytest.mark.asyncio
async def test_environment_provider_failure_does_not_fall_back() -> None:
    class Plugin:
        @hookimpl
        def provide_execution_environment(self, session_id, state):
            raise RuntimeError("unavailable")

    framework = BubFramework()
    framework.plugin_manager.register(Plugin())
    with pytest.raises(RuntimeError, match="unavailable"):
        await framework.get_execution_environment("s", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["default", "container"])
async def test_switch_requires_explicit_authorization_even_when_cached(reference) -> None:
    framework = BubFramework()
    environment = LocalExecutionEnvironment()
    allowed = True

    class Plugin:
        @hookimpl
        def provide_execution_environment(self, session_id, state):
            return environment if allowed else None

    framework.plugin_manager.register(Plugin())
    state = {"environment": reference}
    assert await framework.get_execution_environment("s", state) is environment
    allowed = False
    with pytest.raises(ValueError, match="unauthorized"):
        await framework.prepare_environment_switch("s", state, reference)
    assert state == {"environment": reference}


@pytest.mark.asyncio
async def test_named_environment_never_falls_back_to_host() -> None:
    framework = BubFramework()
    with pytest.raises(ValueError, match="unavailable"):
        await framework.get_execution_environment("s", {"environment": "missing"})
    with pytest.raises(ValueError, match="unauthorized"):
        await framework.prepare_environment_switch("s", {}, "default")


@pytest.mark.asyncio
async def test_environment_sessions_keep_workspaces_separate(tmp_path) -> None:
    framework = BubFramework()
    first = await framework.get_execution_environment("session", {"_runtime_workspace": str(tmp_path / "first")})
    second = await framework.get_execution_environment("session", {"_runtime_workspace": str(tmp_path / "second")})
    from bub.builtin.environment import bind_turn_tools
    from bub.builtin.tools import fs_read, fs_write
    from bub.tools import ToolContext

    # Resource placement is observable; cache identity is not the contract.
    first_tools = bind_turn_tools({"fs.write": fs_write}, first)
    second_tools = bind_turn_tools({"fs.read": fs_read}, second)
    from unittest.mock import MagicMock

    context = ToolContext(tape=MagicMock(), run_id="test", state={"_runtime_execution_environment": first})
    await first_tools["fs.write"].run(path="data.txt", content="first", context=context)
    context.state["_runtime_execution_environment"] = second
    with pytest.raises(FileNotFoundError):
        await second_tools["fs.read"].run(path="data.txt", context=context)
