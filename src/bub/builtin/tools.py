from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Iterable
from contextlib import aclosing
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, TypedDict, cast, final

from pydantic import BaseModel, Field

from bub.builtin.environment import environment_from_state
from bub.builtin.settings import load_settings, set_session_setting
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


def _tool_name_index(all_names: Iterable[str]) -> dict[str, str]:
    names = tuple(all_names)
    real_names = {tool_name.casefold(): tool_name for tool_name in names}
    alias_names = {_to_model_name(tool_name).casefold(): tool_name for tool_name in names}
    return {**alias_names, **real_names}


def resolve_tool_name(name: str) -> str | None:
    """Resolve a user/model-provided tool name to the runtime registry name."""
    key = name.strip().casefold()
    if not key:
        return None
    return _tool_name_index(REGISTRY).get(key)


def _resolve_explicit_tool_names(names: Iterable[str], index: dict[str, str]) -> tuple[set[str], set[str]]:
    resolved: set[str] = set()
    unknown: set[str] = set()
    for name in names:
        normalized_name = name.strip()
        if resolved_name := index.get(normalized_name.casefold()):
            resolved.add(resolved_name)
        else:
            unknown.add(normalized_name)
    return resolved, unknown


def _raise_unknown_tool_names(names: set[str]) -> None:
    formatted = ", ".join(sorted(repr(name) for name in names))
    raise ValueError(f"unknown tool name(s): {formatted}")


def resolve_tool_names(
    names: Iterable[str] | None = None, *, exclude: Iterable[str] = (), all_names: Iterable[str] | None = None
) -> set[str]:
    """Resolve tool names from either runtime names or model-facing aliases."""
    available = tuple(REGISTRY if all_names is None else all_names)
    index = _tool_name_index(available)
    excluded, unknown_excluded = _resolve_explicit_tool_names(exclude, index)
    if unknown_excluded:
        _raise_unknown_tool_names(unknown_excluded)
    if names is None:
        return set(available) - excluded

    resolved, unknown = _resolve_explicit_tool_names(names, index)
    if unknown:
        _raise_unknown_tool_names(unknown)
    return resolved - excluded


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
    agent_tools = [tool_item for tool_item in tools if tool_item.agent_use]
    if not agent_tools:
        return ""
    lines = []
    for tool_item in agent_tools:
        line = f"- {_tool_signature(tool_item)}"
        if tool_item.description:
            line += f": {tool_item.description}"
        lines.append(line)
    return f"<available_tools>\n{'\n'.join(lines)}\n</available_tools>"


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


@final
class SkillList(TypedDict):
    skills: list[str]


@final
class SkillContent(TypedDict):
    name: str
    location: str
    content: str


@final
class ToolFailure(TypedDict):
    """A recoverable failure reported to the caller instead of raising."""

    error: str


class TapeInfoResult(TypedDict):
    name: str
    entries: int
    anchors: int
    last_anchor: str | None
    entries_since_last_anchor: int
    last_token_usage: int | None
    last_token_cache_hit_rate: float | None


class TapeSearchMatch(TypedDict):
    date: str
    content: dict[str, Any]


class TapeSearchResult(TypedDict):
    matches: list[TapeSearchMatch]
    filtered: int


class WebFetchResult(TypedDict):
    url: str
    status: int
    content_type: str
    content: str


class SubAgentResult(TypedDict):
    session_id: str
    output: str
    errors: list[str]


def _render_skill(result: SkillList | SkillContent | ToolFailure) -> str:
    if "error" in result:
        return f"({result['error']})"
    if "skills" in result:
        return "Available skills:\n" + "\n".join(f"- {name}" for name in result["skills"])
    return f"Location: {result['location']}\n---\n{result['content'] or '(no content)'}"


def _render_tape_info(result: TapeInfoResult) -> str:
    hit_rate = result["last_token_cache_hit_rate"]
    cache_hit_rate = f"{hit_rate:.2%}" if hit_rate is not None else "None"
    return (
        f"name: {result['name']}\n"
        f"entries: {result['entries']}\n"
        f"anchors: {result['anchors']}\n"
        f"last_anchor: {result['last_anchor']}\n"
        f"entries_since_last_anchor: {result['entries_since_last_anchor']}\n"
        f"last_token_usage: {result['last_token_usage']}\n"
        f"last_token_cache_hit_rate: {cache_hit_rate}"
    )


def _render_tape_search(result: TapeSearchResult) -> str:
    matches = result["matches"]
    return f"[tape.search]: {len(matches)} matches ({result['filtered']} filtered)" + "".join(
        f"\n{json.dumps(match)}" for match in matches
    )


def _render_anchors(result: dict[str, list[str]]) -> str:
    if not result["anchors"]:
        return "(no anchors)"
    return "\n".join(f"- {name}" for name in result["anchors"])


def _render_subagent(result: SubAgentResult) -> str:
    return result["output"] + "".join(f"[Error: {message}]" for message in result["errors"])


@tool(context=True, preserve=True)
async def bash(
    command: str,
    cwd: str | None = None,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    background: bool = False,
    *,
    context: ToolContext,
) -> str:
    """Run a shell command. Use background=true to keep it running and fetch output later via bash_output.

    Foreground commands that exceed timeout_seconds continue in the background
    and return a shell ID. Use bash.output to read output or bash.kill to stop them.
    Background commands do not use timeout_seconds.
    """
    environment = environment_from_state(context.state)
    target_cwd = environment.resolve_path(cwd) if cwd else None
    raw_session_id = context.state.get("session_id")
    session_id = str(raw_session_id) if raw_session_id is not None else None
    shell = await shell_manager.start(cmd=command, cwd=target_cwd, session_id=session_id, environment=environment)
    if background:
        return f"Shell started, shell_id: {shell.shell_id}\nRetrieve the output with bash_output or terminate it with bash_kill."
    try:
        async with asyncio.timeout(timeout_seconds):
            shell = await shell_manager.wait_closed(shell.shell_id)
    except asyncio.CancelledError:
        with contextlib.suppress(KeyError):
            await shell_manager.terminate(shell.shell_id)
        raise
    except TimeoutError:
        # Cancellation during descendant cleanup waits for termination to finish.
        # A released shell must not be advertised as a running background command.
        if shell.termination_task is None or not shell.termination_task.done():
            return f"command timed out after {timeout_seconds} seconds; continuing in background\nshell_id: {shell.shell_id}"
    _raise_for_failed_shell(shell.returncode, shell.output)
    return shell.output.strip() or "(no output)"


@tool(name="bash.output", preserve=True)
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


@tool(name="bash.kill", preserve=True)
async def kill_bash(shell_id: str) -> str:
    """Terminate a background shell process."""
    shell = await shell_manager.terminate(shell_id)
    return f"id: {shell.shell_id}\nstatus: {shell.status}\nexit_code: {shell.returncode}"


@tool(context=True, name="fs.read", preserve=True)
async def fs_read(path: str, offset: int = 0, limit: int | None = None, *, context: ToolContext) -> str:
    """Read a text file and return its content. Supports optional pagination with offset and limit."""
    environment = environment_from_state(context.state)
    text = await environment.read_text(environment.resolve_path(path))
    lines = text.splitlines()
    start = max(0, min(offset, len(lines)))
    end = len(lines) if limit is None else min(len(lines), start + max(0, limit))
    return "\n".join(lines[start:end])


@tool(context=True, name="fs.write", preserve=True)
async def fs_write(path: str, content: str, *, context: ToolContext) -> str:
    """Write content to a text file."""
    environment = environment_from_state(context.state)
    resolved_path = environment.resolve_path(path)
    await environment.write_text(resolved_path, content)
    return f"wrote: {resolved_path}"


@tool(context=True, name="fs.edit", preserve=True)
async def fs_edit(path: str, old: str, new: str, start: int = 0, *, context: ToolContext) -> str:
    """Edit a text file by replacing old text with new text. You can specify the line number to start searching for the old text."""
    environment = environment_from_state(context.state)
    resolved_path = environment.resolve_path(path)
    text = await environment.read_text(resolved_path)
    lines = text.splitlines()
    prev, to_replace = "\n".join(lines[:start]), "\n".join(lines[start:])
    if old not in to_replace:
        raise ValueError(f"'{old}' not found in {resolved_path} from line {start}")
    replaced = to_replace.replace(old, new)
    if prev:
        replaced = prev + "\n" + replaced
    await environment.write_text(resolved_path, replaced)
    return f"edited: {resolved_path}"


@tool(context=True, name="skill", renderer=_render_skill)
def skill_describe(name: str | None = None, *, context: ToolContext) -> SkillList | SkillContent | ToolFailure:
    """Load the skill content by name. Return the location and skill content.
    If name is not provided, list all available skills in the current workspace.
    """
    from bub.utils import workspace_from_state

    agent = _get_agent(context)
    allowed_skills = context.state.get("allowed_skills")
    if allowed_skills is not None and name and name.casefold() not in allowed_skills:
        return {"error": f"skill '{name}' is not allowed in this context"}

    workspace = workspace_from_state(context.state)
    skill_index = {skill.name: skill for skill in discover_skills(workspace, skill_dirs=agent.skill_dirs)}
    if name is None:
        return {"skills": list(skill_index)}
    if name.casefold() not in skill_index:
        return {"error": "no such skill"}
    skill = skill_index[name.casefold()]
    return {"name": skill.name, "location": str(skill.location), "content": skill.body() or ""}


@tool(context=True, name="tape.info", renderer=_render_tape_info)
async def tape_info(context: ToolContext) -> TapeInfoResult:
    """Get information about the current tape, such as number of entries and anchors."""
    return cast(TapeInfoResult, asdict(await context.tape.info()))


@tool(context=True, name="tape.search", model=SearchInput, renderer=_render_tape_search)
async def tape_search(param: SearchInput, *, context: ToolContext) -> TapeSearchResult:
    """Search for entries in the current tape that match the query. Returns a list of matching entries."""
    query = context.tape.query().query(param.query).kinds(*param.kinds).limit(param.limit)
    if param.start or param.end:
        query = query.between_dates(param.start or "", param.end or "")

    entries = await context.tape.search(query)
    matches: list[TapeSearchMatch] = []
    for entry in entries:
        match: TapeSearchMatch = {"date": entry.date, "content": entry.payload}
        if "[tape.search]" in json.dumps(match):
            continue
        matches.append(match)
    return {"matches": matches, "filtered": len(entries) - len(matches)}


@tool(context=True, name="tape.reset", renderer=lambda result: result["message"])
async def tape_reset(archive: bool = False, *, context: ToolContext) -> dict[str, str]:
    """Reset the current tape, optionally archiving it."""
    return {"message": await context.tape.reset(archive=archive)}


@tool(context=True, name="tape.handoff", renderer=lambda result: f"anchor added: {result['anchor']}")
async def tape_handoff(name: str = "handoff", summary: str = "", *, context: ToolContext) -> dict[str, str]:
    """Add a handoff anchor to the current tape."""
    await context.tape.handoff(name=name, state={"summary": summary})
    return {"anchor": name}


@tool(context=True, name="tape.anchors", renderer=_render_anchors)
async def tape_anchors(*, context: ToolContext) -> dict[str, list[str]]:
    """List anchors in the current tape."""
    anchors = await context.tape.anchors()
    return {"anchors": [anchor.name for anchor in anchors]}


@tool(name="web.fetch", renderer=lambda result: result["content"])
async def web_fetch(url: str, headers: dict | None = None, timeout: int | None = None) -> WebFetchResult:
    """Fetch(GET) the content of a web page, returning markdown if possible."""
    import aiohttp

    headers = {**DEFAULT_HEADERS, **(headers or {})}
    timeout = timeout or DEFAULT_REQUEST_TIMEOUT_SECONDS

    async with (
        aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as session,
        session.get(url) as response,
    ):
        response.raise_for_status()
        return {
            "url": str(response.url),
            "status": response.status,
            "content_type": response.content_type,
            "content": await response.text(),
        }


@tool(name="subagent", context=True, model=SubAgentInput, renderer=_render_subagent)
async def run_subagent(param: SubAgentInput, *, context: ToolContext) -> SubAgentResult:
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
    allowed_tools = resolve_tool_names(
        param.allowed_tools or None, exclude={"subagent"}, all_names=agent.tools | agent.tool_catalog
    )
    output = ""
    errors: list[str] = []
    stream = await agent.run_stream(
        session_id=subagent_session,
        prompt=param.prompt,
        state=state,
        model=param.model,
        allowed_tools=allowed_tools,
        allowed_skills=param.allowed_skills,
    )
    async with aclosing(stream):
        async for event in stream:
            if event.kind == "error":
                errors.append(str(event.data.get("message", "unknown error")))
            elif event.kind == "text":
                output += str(event.data.get("delta", ""))
    return {"session_id": subagent_session, "output": output, "errors": errors}


@tool(name="help", context=True, agent_use=False)
def show_help(*, context: ToolContext | None = None) -> str:
    """Show a help message."""
    agent = context.state.get("_runtime_agent") if context is not None else None
    prefix = agent.command_prefix if agent is not None else load_settings().command_prefix
    return (
        f"Commands use '{prefix}' at line start.\n"
        "Known internal commands:\n"
        f"  {prefix}help\n"
        f"  {prefix}skill name=foo\n"
        f"  {prefix}tape.info\n"
        f"  {prefix}tape.search query=error\n"
        f"  {prefix}tape.handoff name=phase-1 summary='done'\n"
        f"  {prefix}tape.anchors\n"
        f"  {prefix}fs.read path=README.md\n"
        f"  {prefix}fs.write path=tmp.txt content='hello'\n"
        f"  {prefix}fs.edit path=tmp.txt old=hello new=world\n"
        f"  {prefix}bash command='sleep 5' background=true\n"
        f"  {prefix}bash.output shell_id=bsh-12345678\n"
        f"  {prefix}bash.kill shell_id=bsh-12345678\n"
        f"  {prefix}code_mode enable=true\n"
        f"  {prefix}quit\n"
        f"Any unknown command after '{prefix}' is executed as shell via bash."
    )


@tool(name="quit", context=True, agent_use=False)
async def quit_tool(*, context: ToolContext) -> str:
    """Abort the tasks of the current session. DO NOT use it in a normal workflow."""
    agent = _get_agent(context)
    session_id = str(context.state.get("session_id", "temp/unknown"))
    await shell_manager.terminate_session(session_id)
    await agent.framework.quit_via_channel_router(session_id)
    return "Session tasks stopped."


@tool(name="model", context=True, agent_use=False)
async def set_model(model_id: str, *, context: ToolContext) -> str:
    """Switch the model for THIS session. Invoke as the `,model <model_id>` command.

    Takes effect on the NEXT turn and persists across restarts. Pass any
    ``provider:model`` string (for example ``openai:gpt-4o`` or
    ``openrouter:openrouter/free``). An invalid model surfaces as an error on the
    next turn — run `,model <valid_id>` again to recover.
    """
    await set_session_setting(context, "model", model_id)
    return f"Session model set to {model_id} (applies from the next turn)."


@tool(name="reasoning_effort", context=True, agent_use=False)
async def set_reasoning_effort(reasoning_effort: str, *, context: ToolContext) -> str:
    """Set the reasoning effort for this session starting from the next turn."""
    reasoning_effort = reasoning_effort.strip()
    if not reasoning_effort:
        raise ValueError("reasoning_effort must not be empty")
    await set_session_setting(context, "reasoning_effort", reasoning_effort)
    return f"Session reasoning effort set to {reasoning_effort} (applies from the next turn)."
