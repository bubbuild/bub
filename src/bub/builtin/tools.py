from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from openai.types.chat import ChatCompletionToolParam
from pydantic import BaseModel, Field

from bub.builtin.shell_manager import shell_manager
from bub.skills import discover_skills
from bub.tools import REGISTRY, Tool, ToolContext, tool

if TYPE_CHECKING:
    from bub.builtin.agent import Agent

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
DEFAULT_HEADERS = {"accept": "text/markdown"}
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10


def _to_model_name(name: str) -> str:
    return name.replace(".", "_")


def _tool_name_index() -> dict[str, str]:
    real_names = {tool_name.casefold(): tool_name for tool_name in REGISTRY}
    alias_names = {_to_model_name(tool_name).casefold(): tool_name for tool_name in REGISTRY}
    return {**alias_names, **real_names}


def resolve_tool_name(name: str) -> str | None:
    """Resolve a user/model-provided tool name to the runtime registry name."""
    key = name.strip().casefold()
    if not key:
        return None
    return _tool_name_index().get(key)


def _resolve_explicit_tool_names(names: Iterable[str]) -> tuple[set[str], set[str]]:
    resolved: set[str] = set()
    unknown: set[str] = set()
    for name in names:
        normalized_name = name.strip()
        if resolved_name := resolve_tool_name(normalized_name):
            resolved.add(resolved_name)
        else:
            unknown.add(normalized_name)
    return resolved, unknown


def _raise_unknown_tool_names(names: set[str]) -> None:
    formatted = ", ".join(sorted(repr(name) for name in names))
    raise ValueError(f"unknown tool name(s): {formatted}")


def resolve_tool_names(names: Iterable[str] | None = None, *, exclude: Iterable[str] = ()) -> set[str]:
    """Resolve tool names from either runtime names or model-facing aliases."""
    excluded, unknown_excluded = _resolve_explicit_tool_names(exclude)
    if unknown_excluded:
        _raise_unknown_tool_names(unknown_excluded)
    if names is None:
        return set(REGISTRY) - excluded

    resolved, unknown = _resolve_explicit_tool_names(names)
    if unknown:
        _raise_unknown_tool_names(unknown)
    return resolved - excluded


def model_tools(tools: Iterable[Tool]) -> list[Tool]:
    """Convert runtime tool names into model-safe aliases."""
    return [replace(tool_item, name=_to_model_name(tool_item.name)) for tool_item in tools]


def _tool_signature(tool_item: Tool) -> str:
    properties = tool_item.parameters.get("properties", {})
    if not isinstance(properties, dict) or not properties:
        return f"{_to_model_name(tool_item.name)}()"

    required = tool_item.parameters.get("required", [])
    required_names = set(required) if isinstance(required, list) else set()
    params = [name if name in required_names else f"{name}?" for name in properties]
    return f"{_to_model_name(tool_item.name)}({', '.join(params)})"


def render_tools_prompt(tools: Iterable[Tool]) -> str:
    """Render a human-readable description of tools for builtin agent prompts."""
    if not tools:
        return ""
    lines = []
    for tool_item in tools:
        line = f"- {_tool_signature(tool_item)}"
        if tool_item.description:
            line += f": {tool_item.description}"
        lines.append(line)
    return f"<available_tools>\n{'\n'.join(lines)}\n</available_tools>"


def completion_tools(tools: Iterable[Tool]) -> list[ChatCompletionToolParam]:
    """Build any-llm completion tool payloads from Bub tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool_item.name,
                "description": tool_item.description,
                "parameters": tool_item.parameters,
            },
        }
        for tool_item in tools
    ]


def _raise_for_failed_shell(returncode: int | None, output: str) -> None:
    if returncode in (None, 0):
        return

    body = output.strip() or "(no output)"
    raise RuntimeError(f"command exited with code {returncode}\noutput:\n{body}")


def _get_agent(context: ToolContext) -> Agent:
    if "_runtime_agent" not in context.state:
        raise RuntimeError("no runtime agent found in tool context")
    return cast("Agent", context.state["_runtime_agent"])


class SearchInput(BaseModel):
    query: str = Field(..., description="The search query string.")
    limit: int = Field(20, description="Maximum number of search results to return.")
    start: str | None = Field(None, description="Optional start date to filter entries (ISO format).")
    end: str | None = Field(None, description="Optional end date to filter entries (ISO format).")
    kinds: list[str] = Field(
        default=["message", "tool_result"],
        description="Optional list of entry kinds to filter search results. Can include 'event', 'anchor', 'system', 'message', 'tool_call', 'tool_result', 'error'.",
    )


class SubAgentInput(BaseModel):
    prompt: str | list[dict] = Field(
        ..., description="The initial prompt for the sub-agent, either as a string or a list of message parts."
    )
    model: str | None = Field(None, description="The model to use for the sub-agent.")
    session: str = Field(
        "temp",
        description="The session handling strategy for the sub-agent. 'inherit' to use the same session, 'temp' to create a temporary session.",
    )
    allowed_tools: list[str] | None = Field(
        None,
        description="Optional list of allowed tool names for the sub-agent. If not specified, the sub-agent can use any tool available to the main agent.",
    )
    allowed_skills: list[str] | None = Field(
        None,
        description="Optional list of allowed skill names for the sub-agent. If not specified, the sub-agent can use any skill available to the main agent.",
    )


@tool(context=True)
async def bash(
    cmd: str,
    cwd: str | None = None,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    background: bool = False,
    *,
    context: ToolContext,
) -> str:
    """Run a shell command. Use background=true to keep it running and fetch output later via bash_output."""
    workspace = context.state.get("_runtime_workspace")
    target_cwd = cwd or workspace
    raw_session_id = context.state.get("session_id")
    session_id = str(raw_session_id) if raw_session_id is not None else None
    shell = await shell_manager.start(cmd=cmd, cwd=target_cwd, session_id=session_id)
    if background:
        return f"started: {shell.shell_id}"
    try:
        async with asyncio.timeout(timeout_seconds):
            shell = await shell_manager.wait_closed(shell.shell_id)
    except asyncio.CancelledError:
        await shell_manager.terminate(shell.shell_id)
        raise
    except TimeoutError:
        await shell_manager.terminate(shell.shell_id)
        return f"command timed out after {timeout_seconds} seconds and was terminated"
    _raise_for_failed_shell(shell.returncode, shell.output)
    return shell.output.strip() or "(no output)"


@tool(name="bash.output")
async def bash_output(shell_id: str, offset: int = 0, limit: int | None = None) -> str:
    """Read buffered output from a background shell, with optional offset/limit for incremental polling."""
    shell = shell_manager.get(shell_id)
    if shell.returncode is not None:
        await shell_manager.wait_closed(shell_id)
    output = shell.output
    start = max(0, min(offset, len(output)))
    end = len(output) if limit is None else min(len(output), start + max(0, limit))
    chunk = output[start:end].rstrip()
    exit_code = "null" if shell.returncode is None else str(shell.returncode)
    body = chunk or "(no output)"
    return f"id: {shell.shell_id}\nstatus: {shell.status}\nexit_code: {exit_code}\nnext_offset: {end}\noutput:\n{body}"


@tool(name="bash.kill")
async def kill_bash(shell_id: str) -> str:
    """Terminate a background shell process."""
    shell = shell_manager.get(shell_id)
    if shell.returncode is None:
        shell = await shell_manager.terminate(shell_id)
    else:
        await shell_manager.wait_closed(shell_id)
    return f"id: {shell.shell_id}\nstatus: {shell.status}\nexit_code: {shell.returncode}"


@tool(context=True, name="fs.read")
def fs_read(path: str, offset: int = 0, limit: int | None = None, *, context: ToolContext) -> str:
    """Read a text file and return its content. Supports optional pagination with offset and limit."""
    resolved_path = _resolve_path(context, path)
    text = resolved_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = max(0, min(offset, len(lines)))
    end = len(lines) if limit is None else min(len(lines), start + max(0, limit))
    return "\n".join(lines[start:end])


@tool(context=True, name="fs.write")
def fs_write(path: str, content: str, *, context: ToolContext) -> str:
    """Write content to a text file."""
    resolved_path = _resolve_path(context, path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(content, encoding="utf-8")
    return f"wrote: {resolved_path}"


@tool(context=True, name="fs.edit")
def fs_edit(path: str, old: str, new: str, start: int = 0, *, context: ToolContext) -> str:
    """Edit a text file by replacing old text with new text. You can specify the line number to start searching for the old text."""
    resolved_path = _resolve_path(context, path)
    text = resolved_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    prev, to_replace = "\n".join(lines[:start]), "\n".join(lines[start:])
    if old not in to_replace:
        raise ValueError(f"'{old}' not found in {resolved_path} from line {start}")
    replaced = to_replace.replace(old, new)
    if prev:
        replaced = prev + "\n" + replaced
    resolved_path.write_text(replaced, encoding="utf-8")
    return f"edited: {resolved_path}"


@tool(context=True, name="skill")
def skill_describe(name: str, *, context: ToolContext) -> str:
    """Load the skill content by name. Return the location and skill content."""
    from bub.utils import workspace_from_state

    allowed_skills = context.state.get("allowed_skills")
    if allowed_skills is not None and name.casefold() not in allowed_skills:
        return f"(skill '{name}' is not allowed in this context)"

    workspace = workspace_from_state(context.state)
    skill_index = {skill.name: skill for skill in discover_skills(workspace)}
    if name.casefold() not in skill_index:
        return "(no such skill)"
    skill = skill_index[name.casefold()]
    return f"Location: {skill.location}\n---\n{skill.body() or '(no content)'}"


@tool(context=True, name="tape.info")
async def tape_info(context: ToolContext) -> str:
    """Get information about the current tape, such as number of entries and anchors."""
    info = await context.tape.info()
    return (
        f"name: {info.name}\n"
        f"entries: {info.entries}\n"
        f"anchors: {info.anchors}\n"
        f"last_anchor: {info.last_anchor}\n"
        f"entries_since_last_anchor: {info.entries_since_last_anchor}\n"
        f"last_token_usage: {info.last_token_usage}"
    )


@tool(context=True, name="tape.search", model=SearchInput)
async def tape_search(param: SearchInput, *, context: ToolContext) -> str:
    """Search for entries in the current tape that match the query. Returns a list of matching entries."""
    query = context.tape.query().query(param.query).kinds(*param.kinds).limit(param.limit)
    if param.start or param.end:
        query = query.between_dates(param.start or "", param.end or "")

    entries = await context.tape.search(query)
    lines: list[str] = []
    for entry in entries:
        entry_str = json.dumps({"date": entry.date, "content": entry.payload})
        if "[tape.search]" in entry_str:
            continue
        lines.append(entry_str)
    return f"[tape.search]: {len(lines)} matches ({len(entries) - len(lines)} filtered)" + "".join(
        f"\n{line}" for line in lines
    )


@tool(context=True, name="tape.reset")
async def tape_reset(archive: bool = False, *, context: ToolContext) -> str:
    """Reset the current tape, optionally archiving it."""
    result = await context.tape.reset(archive=archive)
    return result


@tool(context=True, name="tape.handoff")
async def tape_handoff(name: str = "handoff", summary: str = "", *, context: ToolContext) -> str:
    """Add a handoff anchor to the current tape."""
    await context.tape.handoff(name=name, state={"summary": summary})
    return f"anchor added: {name}"


@tool(context=True, name="tape.anchors")
async def tape_anchors(*, context: ToolContext) -> str:
    """List anchors in the current tape."""
    anchors = await context.tape.anchors()
    if not anchors:
        return "(no anchors)"
    return "\n".join(f"- {anchor.name}" for anchor in anchors)


@tool(name="web.fetch")
async def web_fetch(url: str, headers: dict | None = None, timeout: int | None = None) -> str:
    """Fetch(GET) the content of a web page, returning markdown if possible."""
    import aiohttp

    headers = {**DEFAULT_HEADERS, **(headers or {})}
    timeout = timeout or DEFAULT_REQUEST_TIMEOUT_SECONDS

    async with (
        aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as session,
        session.get(url) as response,
    ):
        response.raise_for_status()
        return await response.text()


@tool(name="subagent", context=True, model=SubAgentInput)
async def run_subagent(param: SubAgentInput, *, context: ToolContext) -> str:
    """Run a task with sub-agent using specific model and session."""
    agent = _get_agent(context)
    session_id = context.state.get("session_id", "temp/unknown")
    if param.session == "inherit":
        subagent_session = session_id
    elif param.session == "temp":
        subagent_session = f"temp/{uuid.uuid4().hex[:8]}"
    else:
        subagent_session = param.session
    state = {**context.state, "session_id": subagent_session}
    allowed_tools = resolve_tool_names(param.allowed_tools or None, exclude={"subagent"})
    output = ""
    async for event in await agent.run_stream(
        session_id=subagent_session,
        prompt=param.prompt,
        state=state,
        model=param.model,
        allowed_tools=allowed_tools,
        allowed_skills=param.allowed_skills,
    ):
        if event.kind == "error":
            output += f"[Error: {event.data.get('message', 'unknown error')}]"
        elif event.kind == "text":
            output += str(event.data.get("delta", ""))
    return output


@tool(name="help")
def show_help() -> str:
    """Show a help message."""
    return (
        "Commands use ',' at line start.\n"
        "Known internal commands:\n"
        "  ,help\n"
        "  ,skill name=foo\n"
        "  ,tape.info\n"
        "  ,tape.search query=error\n"
        "  ,tape.handoff name=phase-1 summary='done'\n"
        "  ,tape.anchors\n"
        "  ,fs.read path=README.md\n"
        "  ,fs.write path=tmp.txt content='hello'\n"
        "  ,fs.edit path=tmp.txt old=hello new=world\n"
        "  ,bash cmd='sleep 5' background=true\n"
        "  ,bash.output shell_id=bsh-12345678\n"
        "  ,bash.kill shell_id=bsh-12345678\n"
        "  ,quit\n"
        "Any unknown command after ',' is executed as shell via bash."
    )


@tool(name="quit", context=True)
async def quit_tool(*, context: ToolContext) -> str:
    """Abort the tasks of the current session. DO NOT use it in a normal workflow."""
    agent = _get_agent(context)
    session_id = str(context.state.get("session_id", "temp/unknown"))
    await shell_manager.terminate_session(session_id)
    await agent.framework.quit_via_router(session_id)
    return "Session tasks stopped."


@tool(name="model", context=True)
async def set_model(model_id: str, *, context: ToolContext) -> str:
    """Switch the model for THIS session. Invoke as the `,model <model_id>` command.

    Takes effect on the NEXT turn and persists across restarts. Pass any
    ``provider:model`` string (for example ``openai:gpt-4o`` or
    ``openrouter:openrouter/free``). An invalid model surfaces as an error on the
    next turn — run `,model <valid_id>` again to recover.
    """
    context.state["model"] = model_id
    # Persist on the session tape (merged back at end of turn); load_state
    # recovers the latest `model_switch` event next turn / after restart.
    await context.tape.append_event("model_switch", {"model": model_id})
    return f"Session model set to {model_id} (applies from the next turn)."


def _resolve_path(context: ToolContext, raw_path: str) -> Path:
    workspace = context.state.get("_runtime_workspace")
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    if workspace is None:
        raise ValueError(f"relative path '{raw_path}' is not allowed without a workspace")
    if not isinstance(workspace, str | Path):
        raise TypeError("runtime workspace must be a filesystem path")
    workspace_path = Path(workspace)
    return (workspace_path / path).resolve()
