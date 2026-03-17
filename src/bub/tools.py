import inspect
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any, Literal, overload

from loguru import logger
from pydantic import BaseModel
from republic import Tool
from republic import tool as republic_tool

# Valid effect kind strings for the @tool(effect=...) parameter.
EffectKindStr = Literal["ReadOnly", "IdempotentWrite", "Compensatable", "IrreversibleWrite", "ReadThenWrite"]

# Central registry for tools. Tools defined with the @tool decorator are automatically added here.
REGISTRY: dict[str, Tool] = {}

# Effect kind registry. Maps tool name → effect kind string (e.g. "ReadOnly", "IrreversibleWrite").
# Populated when @tool(effect="...") is used. Consumed by enable_effect_log().
EFFECT_KINDS: dict[str, str] = {}


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

    wrapped.__wrapped__ = tool.handler
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
    effect: EffectKindStr | None = ...,
) -> Tool: ...


@overload
def tool(
    func: None = ...,
    *,
    name: str | None = ...,
    model: type[BaseModel] | None = ...,
    description: str | None = ...,
    context: bool = ...,
    effect: EffectKindStr | None = ...,
) -> Callable[[Callable], Tool]: ...


def tool(
    func: Callable | None = None,
    *,
    name: str | None = None,
    model: type[BaseModel] | None = None,
    description: str | None = None,
    context: bool = False,
    effect: EffectKindStr | None = None,
) -> Tool | Callable[[Callable], Tool]:
    """Decorator to convert a function into a Tool instance.

    Args:
        func: The function to wrap.
        name: Override the tool name (default: derived from function name).
        model: Pydantic model for structured input.
        description: Override the tool description.
        context: Whether the tool receives a ToolContext.
        effect: Semantic effect kind for crash recovery via effect-log.
            One of: "ReadOnly", "IdempotentWrite", "Compensatable",
            "IrreversibleWrite", "ReadThenWrite". When set, the tool is
            eligible for WAL-based crash recovery if effect-log is enabled.
    """

    result = republic_tool(
        func=func,
        name=name,
        model=model,
        description=description,
        context=context,
    )
    if isinstance(result, Tool):
        REGISTRY[result.name] = result
        if effect is not None:
            EFFECT_KINDS[result.name] = effect
        return _add_logging(result)

    def decorator(func: Callable) -> Tool:
        tool_instance = _add_logging(result(func))
        REGISTRY[tool_instance.name] = tool_instance
        if effect is not None:
            EFFECT_KINDS[tool_instance.name] = effect
        return tool_instance

    return decorator


def unwrap_handler(handler: Callable) -> Callable:
    """Strip logging and pydantic wrappers to get the raw function.

    The wrapping chain is: logging_wrapper → republic (pydantic validate_call) → raw.
    ``_add_logging`` sets ``__wrapped__`` → republic handler.
    Republic's validate_call sets ``raw_function`` / ``__wrapped__`` → raw function.
    """
    # Skip logging wrapper
    raw = getattr(handler, "__wrapped__", handler)
    # Skip pydantic validate_call
    raw = getattr(raw, "raw_function", getattr(raw, "__wrapped__", raw))
    return raw


def _to_model_name(name: str) -> str:
    return name.replace(".", "_")


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


class _EffectLogContext:
    """Shared storage for passing ToolContext through effect-log's execute cycle.

    Uses a plain attribute instead of threading.local because the Rust
    effect-log extension invokes the Python callback from a different OS
    thread (while holding the GIL).  A threading.local would see a fresh
    namespace on that callback thread, losing the stored context.

    Safety: the GIL prevents concurrent Python execution and log.execute()
    is synchronous, so no interleaving can occur between set and read.
    """

    value: Any = None


_effect_log_context = _EffectLogContext()


def make_adapted_handler(raw: Callable, needs_context: bool) -> Callable:
    """Build an adapted handler for effect-log ``ToolDef``.

    Wraps the raw (unwrapped) handler to:
    - inject ``ToolContext`` from ``_effect_log_context`` when the tool requires it,
    - handle async execution in a sync context (as required by effect-log callbacks).
    """

    def adapted(args: dict) -> Any:
        kwargs = dict(args)
        if needs_context:
            ctx = getattr(_effect_log_context, "value", None)
            if ctx is not None:
                kwargs["context"] = ctx

        result = raw(**kwargs)
        if inspect.isawaitable(result):
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(result)
            finally:
                loop.close()
        return result

    return adapted


def _add_effect_log(tool_instance: Tool, log: Any) -> Tool:
    """Wrap a tool's handler to route calls through an effect-log WAL.

    Follows the same pattern as ``_add_logging``: returns a new Tool via
    ``dataclasses.replace()`` with a wrapped handler.
    """
    if tool_instance.handler is None:
        return tool_instance

    # Unwrap any previous effect-log layer to prevent stacking
    original_handler = getattr(tool_instance.handler, "__effect_log_wrapped__", tool_instance.handler)

    async def wrapped(*args, **kwargs):
        call_args = {k: v for k, v in kwargs.items() if k != "context"}
        for i, v in enumerate(args):
            call_args[f"_pos_{i}"] = v

        # Stash context so the adapted handler can retrieve it for replay
        _effect_log_context.value = kwargs.get("context")
        try:
            result = log.execute(tool_instance.name, call_args)
        finally:
            _effect_log_context.value = None

        if isinstance(result, str):
            return result
        try:
            return json.dumps(result)
        except (TypeError, ValueError):
            return str(result)

    wrapped.__effect_log_wrapped__ = original_handler
    return replace(tool_instance, handler=wrapped)


def enable_effect_log(log: Any, registry: dict[str, Tool] | None = None) -> None:
    """Enable effect-log crash recovery for all tools that declared an ``effect``.

    Wraps the handler of each tool whose name appears in ``EFFECT_KINDS``
    so that calls go through the effect-log WAL instead of executing directly.

    Safe to call multiple times (e.g. for different sessions): any existing
    effect-log wrapper is unwrapped before the new one is applied.

    Call this once at startup, after tools are registered and before the
    agent begins processing::

        from effect_log import EffectLog
        from bub.tools import enable_effect_log

        log = EffectLog(execution_id="...", tools=tooldefs, storage="sqlite:///effects.db")
        enable_effect_log(log)

    Args:
        log: An initialized ``EffectLog`` instance.
        registry: Tool registry to wrap. Defaults to the global ``REGISTRY``.
    """
    target = registry if registry is not None else REGISTRY
    for name, tool_instance in list(target.items()):
        if name in EFFECT_KINDS:
            # Restore pre-effect-log handler if previously wrapped
            prev = getattr(tool_instance.handler, "__effect_log_wrapped__", None)
            if prev is not None:
                tool_instance = replace(tool_instance, handler=prev)
            target[name] = _add_effect_log(tool_instance, log)
