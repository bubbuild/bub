"""Default tool catalogs: direct tools first, code-mode presentation last."""

from __future__ import annotations

from dataclasses import dataclass

from bub.tape import Tape
from bub.tools import Tool, model_tools
from bub.utils import workspace_from_state


@dataclass
class BuiltinToolCatalog:
    """Expose the agent's registered tools directly."""

    tools: dict[str, Tool]

    async def prepare(self, tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
        return tools, ""


@dataclass
class CodeModeCatalog:
    """Present the selected tools as a code stub when code mode is enabled."""

    registry: dict[str, Tool]

    @property
    def tools(self) -> dict[str, Tool]:
        return {name: item for name, item in self.registry.items() if name == "run_code"}

    async def prepare(self, tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
        from bub.builtin.codemode import (
            CODE_MODE_STATE_KEY,
            CODE_TOOLS_STATE_KEY,
            RUN_CODE_TOOL_NAME,
            render_code_mode_prompt,
            write_tool_stub,
        )

        state = tape.context.state
        direct_tools = [item for item in tools if item.name != RUN_CODE_TOOL_NAME]
        if not state.get(CODE_MODE_STATE_KEY) or len(direct_tools) == len(tools):
            state.pop(CODE_TOOLS_STATE_KEY, None)
            return direct_tools, ""

        code_tools = [item for item in direct_tools if item.code_use]
        state[CODE_TOOLS_STATE_KEY] = model_tools(code_tools)
        stub_path = write_tool_stub(
            code_tools, session_id=str(state.get("session_id", "")), workspace=workspace_from_state(state)
        )
        return [item for item in tools if item.preserve], render_code_mode_prompt(stub_path)
