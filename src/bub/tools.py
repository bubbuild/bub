from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, overload

from loguru import logger
from pydantic import BaseModel, TypeAdapter, ValidationError, validate_call

from bub.runtime import BubError, ErrorKind


@dataclass(frozen=True)
class ToolContext:
    """Runtime context passed to tools that opt into context."""

    tape: str | None = None
    run_id: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


def _to_snake_case(name: str) -> str:
    return "".join(["_" + c.lower() if c.isupper() else c for c in name]).lstrip("_")


def _callable_name(func: Callable[..., Any]) -> str:
    name = getattr(func, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return func.__class__.__name__


def _schema_from_annotation(annotation: Any) -> dict[str, Any]:
    if annotation is inspect._empty:
        annotation = Any
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception as exc:
        raise ValueError(f"Failed to build JSON schema for type: {annotation!r}") from exc


def _schema_from_signature(signature: inspect.Signature, *, ignore_params: set[str] | None = None) -> dict[str, Any]:
    ignore = ignore_params or set()
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in signature.parameters.values():
        if param.name in ignore:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        properties[param.name] = _schema_from_annotation(param.annotation)
        if param.default is param.empty:
            required.append(param.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


@dataclass(frozen=True)
class Tool:
    """A callable unit the model can invoke."""

    name: str
    handler: Callable[..., Any]
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    context: bool = False

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self.handler(*args, **kwargs)

    @classmethod
    def from_callable(
        cls,
        func: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        context: bool = False,
    ) -> Tool:
        signature = inspect.signature(func)
        if context and "context" not in signature.parameters:
            raise TypeError("Tool context is enabled but the callable lacks a 'context' parameter.")
        tool_name = name or _to_snake_case(_callable_name(func))
        tool_description = description if description is not None else (inspect.getdoc(func) or "")
        parameters = _schema_from_signature(signature, ignore_params={"context"} if context else None)
        validated = validate_call(func)
        return cls(
            name=tool_name,
            description=tool_description,
            parameters=parameters,
            handler=validated,
            context=context,
        )


@dataclass(frozen=True)
class ToolExecution:
    tool_results: list[Any] = field(default_factory=list)
    error: BubError | None = None


class ToolExecutor:
    """Execute already-resolved Bub tool invocations."""

    async def execute_async(
        self,
        invocations: Sequence[tuple[Tool, dict[str, Any]]],
        *,
        context: ToolContext | None = None,
    ) -> ToolExecution:
        if not invocations:
            return ToolExecution(tool_results=[])

        results: list[Any] = []
        error: BubError | None = None
        gathered = await asyncio.gather(
            *(self._handle_tool_response_async(tool_obj, tool_args, context) for tool_obj, tool_args in invocations),
            return_exceptions=True,
        )
        for result in gathered:
            if isinstance(result, BubError):
                error = result
                results.append(result.as_dict())
            elif isinstance(result, BaseException):
                raise result
            else:
                results.append(result)

        return ToolExecution(tool_results=results, error=error)

    def _invoke_tool(
        self,
        *,
        tool_name: str,
        tool_obj: Tool,
        tool_args: dict[str, Any],
        context: ToolContext | None,
    ) -> Any:
        if tool_obj.context:
            if context is None:
                raise BubError(ErrorKind.INVALID_INPUT, f"Tool '{tool_name}' requires context but none was provided.")
            return tool_obj.run(context=context, **tool_args)
        return tool_obj.run(**tool_args)

    async def _handle_tool_response_async(
        self,
        tool_obj: Tool,
        tool_args: dict[str, Any],
        context: ToolContext | None,
    ) -> Any:
        tool_name = tool_obj.name
        try:
            result = self._invoke_tool(
                tool_name=tool_name,
                tool_obj=tool_obj,
                tool_args=tool_args,
                context=context,
            )
            if inspect.isawaitable(result):
                return await result
        except BubError:
            raise
        except ValidationError as exc:
            raise BubError(
                ErrorKind.INVALID_INPUT,
                f"Tool '{tool_name}' argument validation failed.",
                details={"errors": json.loads(exc.json())},
            ) from exc
        except Exception as exc:
            raise BubError(
                ErrorKind.TOOL,
                f"Tool '{tool_name}' execution failed.",
                details={"error": repr(exc)},
            ) from exc
        else:
            return result


# Central registry for tools. Tools defined with the @tool decorator are automatically added here.
REGISTRY: dict[str, Tool] = {}


def _add_logging(tool: Tool) -> Tool:
    handler = tool.handler

    async def wrapped(*args, **kwargs):
        call_kwargs = kwargs.copy()
        if tool.context:
            call_kwargs.pop("context", None)
        _log_tool_call(tool.name, args, call_kwargs)
        start = time.monotonic()

        try:
            result = handler(*args, **kwargs)
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

    def decorator(func: Callable) -> Tool:
        if model is not None:
            if context and "context" not in inspect.signature(func).parameters:
                raise TypeError("Tool context is enabled but the handler lacks a 'context' parameter.")

            def handler(*args: Any, **kwargs: Any) -> Any:
                tool_context = kwargs.pop("context", None)
                parsed = model(*args, **kwargs)
                if context:
                    return func(parsed, context=tool_context)
                return func(parsed)

            result = Tool(
                name=name or _to_snake_case(model.__name__),
                description=description if description is not None else (model.__doc__ or ""),
                parameters=model.model_json_schema(),
                handler=handler,
                context=context,
            )
        else:
            result = Tool.from_callable(func, name=name, description=description, context=context)
        tool_instance = _add_logging(result)
        REGISTRY[tool_instance.name] = tool_instance
        return tool_instance

    if func is None:
        return decorator
    return decorator(func)
