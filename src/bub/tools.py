import inspect
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any, overload

from loguru import logger
from pydantic import BaseModel
from republic import Tool
from republic import tool as republic_tool

# Central registry for tools. Tools defined with the @tool decorator are automatically added here.
REGISTRY: dict[str, Tool] = {}


def _add_logging(tool: Tool) -> Tool:
    if tool.handler is None:
        return tool

    async def wrapped(*args, **kwargs):
        call_kwargs = kwargs.copy()
        if tool.context:
            call_kwargs.pop("context", None)
        _log_tool_call(tool.name, args, call_kwargs)
        start = time.monotonic()

        try:
            result = tool.handler(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            elapsed_time = (time.monotonic() - start) * 1000
            logger.exception("tool.call.error name={} elapsed_time={:.2f}ms", tool.name, elapsed_time)
            raise
        else:
            elapsed_time = (time.monotonic() - start) * 1000
            logger.info("tool.call.success name={} elapsed_time={:.2f}ms", tool.name, elapsed_time)
            return result

    return replace(tool, handler=wrapped)


def _shorten_text(text: str, width: int = 30, placeholder: str = "...") -> str:
    if len(text) <= width:
        return text

    # Reserve space for placeholder
    available = width - len(placeholder)
    if available <= 0:
        return placeholder

    return text[:available] + placeholder


def _render_value(value: Any) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except TypeError:
        rendered = repr(value)
    rendered = _shorten_text(rendered, width=100, placeholder="...")
    if rendered.startswith('"') and not rendered.endswith('"'):
        rendered = rendered + '"'
    if rendered.startswith("{") and not rendered.endswith("}"):
        rendered = rendered + "}"
    if rendered.startswith("[") and not rendered.endswith("]"):
        rendered = rendered + "]"
    return rendered


def _log_tool_call(name: str, args: Any, kwargs: dict[str, Any]) -> None:
    params: list[str] = []

    for value in args:
        params.append(_render_value(value))
    for key, value in kwargs.items():
        rendered = _render_value(value)
        params.append(f"{key}={rendered}")
    params_str = f" {{ {', '.join(params)} }}" if params else ""
    logger.info("tool.call.start name={}{}", name, params_str)


@overload
def tool(
    func: Callable,
    *,
    name: str | None = ...,
    model: type[BaseModel] | None = ...,
    description: str | None = ...,
    context: bool = ...,
) -> Tool: ...


@overload
def tool(
    func: None = ...,
    *,
    name: str | None = ...,
    model: type[BaseModel] | None = ...,
    description: str | None = ...,
    context: bool = ...,
) -> Callable[[Callable], Tool]: ...


def tool(
    func: Callable | None = None,
    *,
    name: str | None = None,
    model: type[BaseModel] | None = None,
    description: str | None = None,
    context: bool = False,
) -> Tool | Callable[[Callable], Tool]:
    """Decorator to convert a function into a Tool instance."""

    result = republic_tool(
        func=func,
        name=name,
        model=model,
        description=description,
        context=context,
    )
    if isinstance(result, Tool):
        tool_instance = _add_logging(result)
        REGISTRY[tool_instance.name] = tool_instance
        return tool_instance

    def decorator(func: Callable) -> Tool:
        tool_instance = _add_logging(result(func))
        REGISTRY[tool_instance.name] = tool_instance
        return tool_instance

    return decorator


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
    """Helper to convert a list of Tool instances into a format accepted by LLMs."""
    return [replace(tool, name=_to_model_name(tool.name)) for tool in tools]


def render_tools_prompt(tools: Iterable[Tool]) -> str:
    """Render a human-readable description of tools for model prompts."""
    if not tools:
        return ""
    lines = []
    for tool in tools:
        line = f"- {_to_model_name(tool.name)}"
        if tool.description:
            line += f": {tool.description}"
        lines.append(line)
    return f"<available_tools>\n{'\n'.join(lines)}\n</available_tools>"
