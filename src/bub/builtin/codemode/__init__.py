"""Bub's code mode: :class:`bub.core.codemode.CodeMode` wired to session settings, environments and models.

``BubAgent`` chooses the code-callable tools of each turn and stores them in the turn
state under ``CODE_TOOLS_STATE_KEY``; the ``code_mode`` command switches it per session.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from bub.builtin.environment import environment_from_state
from bub.builtin.settings import set_session_setting
from bub.core.codemode import (
    DEFAULT_RUN_CODE_TIMEOUT_SECONDS,
    MEDIA_TOOLS,
    RUN_CODE_TOOL_NAME,
    CodeMode,
    attach_audio,
    attach_image,
    attach_video,
    render_code_mode_prompt,
    render_tool_stub,
    write_stub,
)
from bub.core.tools import Tool, ToolContext, tool
from bub.turn import TurnState
from bub.utils import workspace_from_state

__all__ = [
    "CODE_MODE",
    "CODE_MODE_STATE_KEY",
    "CODE_TOOLS_STATE_KEY",
    "DEFAULT_RUN_CODE_TIMEOUT_SECONDS",
    "RUN_CODE_TOOL_NAME",
    "attach_audio",
    "attach_image",
    "attach_video",
    "render_code_mode_prompt",
    "render_tool_stub",
    "run_code",
    "set_code_mode",
    "write_tool_stub",
]

CODE_MODE_STATE_KEY = "code_mode"
CODE_TOOLS_STATE_KEY = "_runtime_code_tools"


def _resolve_model(spec: str | None, context: ToolContext) -> tuple[str, Mapping[str, Any]]:
    """Resolve a spec, or the session's model, with Bub's settings: fallbacks, credentials and API bases."""
    agent = context.state.get("_runtime_agent")
    settings = getattr(agent, "settings", None)
    if settings is None:
        raise ValueError("Republic models need a running Bub agent.")
    candidate = settings.model_candidates(spec or context.state.get("model") or settings.model)[0]
    client_kwargs = settings.model_client_kwargs(candidate.provider_name or candidate.provider)
    return f"{candidate.provider}:{candidate.model_id}", client_kwargs


def _stub_dir(session_id: str, workspace: Path) -> Path:
    import bub

    return bub.home / "codemode" / hashlib.sha256(f"{workspace}\0{session_id}".encode()).hexdigest()[:16]


def _state_stub_dir(state: TurnState) -> Path:
    return _stub_dir(str(state.get("session_id", "")), workspace_from_state(state))


def write_tool_stub(tools: Iterable[Tool], *, session_id: str, workspace: Path) -> Path:
    """Write the tool stub under Bub home; the path stays stable while the session's tool set is unchanged."""
    return write_stub(render_tool_stub(tools), _stub_dir(session_id, workspace))


CODE_MODE = CodeMode(
    lambda state: state.get(CODE_TOOLS_STATE_KEY),
    environment=environment_from_state,
    models=_resolve_model,
    stub_dir=_state_stub_dir,
    # The media tools are among TOOLS, so turns select them like any other tool.
    media_tools=False,
)
"""Code mode of Bub's agents."""

run_code = CODE_MODE.run_code


@tool(name="code_mode", context=True, exposure="command")
async def set_code_mode(enable: bool, *, context: ToolContext) -> str:
    """Enable or disable code mode for THIS session. Invoke as the `,code_mode enable=true` command.

    In code mode the model calls `direct` tools directly and every other tool from
    Python through `run_code`. Takes effect on the NEXT turn and persists across restarts.
    """
    await set_session_setting(context, CODE_MODE_STATE_KEY, enable)
    return f"Session code mode {'enabled' if enable else 'disabled'} (applies from the next turn)."


TOOLS: tuple[Tool, ...] = (run_code, *MEDIA_TOOLS, set_code_mode)
"""Code mode tools the builtin plugin provides."""
