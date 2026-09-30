"""On-demand native definitions for tools that opt into deferred exposure."""

from __future__ import annotations

from collections.abc import Iterable

from bub.tape import Tape
from bub.tools import Tool, ToolContext

DEFERRED_TOOLS_STATE_KEY = "_deferred_tools"
DEFINITIONS_LOADED_EVENT = "tool.definitions.loaded"


async def loaded_tool_names(tape: Tape) -> set[str]:
    """Recover definitions requested in the current tape context, including after restart."""
    query = tape.context.build_query(tape.query()).kinds("event")
    entries = await tape.store.fetch_all(query)
    return {
        name
        for entry in entries
        if entry.payload.get("name") == DEFINITIONS_LOADED_EVENT
        for name in entry.payload.get("data", {}).get("names", [])
        if isinstance(name, str)
    }


async def describe_tools(names: list[str], *, context: ToolContext) -> str:
    from bub.builtin.tools import resolve_tool_names

    available: dict[str, Tool] = context.state.get(DEFERRED_TOOLS_STATE_KEY, {})
    resolved = resolve_tool_names(names, all_names=available)
    if not resolved:
        raise ValueError("provide at least one available tool name")
    await context.tape.append_event(DEFINITIONS_LOADED_EVENT, {"names": sorted(resolved)}, context=False)
    aliases = ", ".join(name.replace(".", "_") for name in sorted(resolved))
    return f"Complete native definitions are now available for: {aliases}. Call these tools directly."


DESCRIBE_TOOL = Tool.from_callable(
    describe_tools,
    name="tool.describe",
    description=(
        "Expose complete native definitions for the named tools in the available_tools catalog on the next model call. "
        "Use exact catalog names; tools whose definitions are already available can be called directly."
    ),
    context=True,
    preserve=True,
)


def render_deferred_tools_prompt(tools: Iterable[Tool]) -> str:
    lines: list[str] = []
    for tool in tools:
        if not tool.agent_use or not tool.defer_loading:
            continue
        description = next((line.strip() for line in tool.description.splitlines() if line.strip()), "")
        summary = description.split(". ", 1)[0][:180]
        name = tool.name.replace(".", "_")
        lines.append(f"- {name}: {summary}" if summary else f"- {name}")
    if not lines:
        return ""
    return (
        "Call tools whose complete native definitions are already available directly. "
        "The catalog below lists tools without native definitions; use tool_describe to obtain them by name.\n"
        f"<available_tools>\n{'\n'.join(lines)}\n</available_tools>"
    )
