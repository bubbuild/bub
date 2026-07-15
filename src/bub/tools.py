from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, overload

from loguru import logger
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, validate_call

from bub.builtin.tape import Tape
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import ToolCall, ToolCallResult

if TYPE_CHECKING:
    from bub.hooks.interception import AgentHooks


@dataclass(frozen=True)
class ToolContext:
    """Runtime context passed to tools that opt into context."""

    tape: Tape
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


def _signature_without_context(signature: inspect.Signature) -> inspect.Signature:
    parameters = [param for param in signature.parameters.values() if param.name != "context"]
    return signature.replace(parameters=parameters)


def _validate_without_context(func: Callable[..., Any], signature: inspect.Signature) -> Callable[..., Any]:
    def validate_target(*args: Any, **kwargs: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
        return args, kwargs

    validate_target.__name__ = _callable_name(func)
    validate_target.__qualname__ = getattr(func, "__qualname__", validate_target.__name__)
    validate_target.__annotations__ = dict(getattr(func, "__annotations__", {}))
    validate_target.__annotations__.pop("context", None)
    validate_target.__signature__ = _signature_without_context(signature)  # type: ignore[attr-defined]
    return validate_call(validate_target)


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
        if context:
            validate_args = _validate_without_context(func, signature)

            def validated(*args: Any, **kwargs: Any) -> Any:
                tool_context = kwargs.pop("context")
                validated_args, validated_kwargs = validate_args(*args, **kwargs)
                return func(*validated_args, context=tool_context, **validated_kwargs)

        else:
            validated = validate_call(config=ConfigDict(arbitrary_types_allowed=True))(func)
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


class ToolCallReporter(Protocol):
    def start(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None: ...

    def success(self, name: str, result: Any, elapsed_ms: float) -> None: ...

    def error(self, name: str, error: BaseException, elapsed_ms: float) -> None: ...


_TOOL_CALL_REPORTER: contextvars.ContextVar[ToolCallReporter | None] = contextvars.ContextVar(
    "bub_tool_call_reporter", default=None
)


@contextlib.contextmanager
def tool_call_reporter(reporter: ToolCallReporter):
    token = _TOOL_CALL_REPORTER.set(reporter)
    try:
        yield
    finally:
        _TOOL_CALL_REPORTER.reset(token)


class ToolExecutor:
    """Execute already-resolved Bub tool invocations."""

    def __init__(self, hooks: AgentHooks | None = None) -> None:
        self._hooks = hooks

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
        call = ToolCall(
            run_id=(context.run_id if context is not None else None) or "",
            tool=tool_name,
            arguments=dict(tool_args),
        )
        hook_state = context.state if context is not None else {}
        started = time.monotonic()
        if self._hooks is not None:
            call, short_circuit = await self._apply_before_tool_call(call, hook_state, started)
            if short_circuit is not None:
                return short_circuit()

        try:
            result = await self._invoke_normalized(tool_obj, call, context)
        except BubError as exc:
            await self._fire_after_tool_call(call, hook_state, started, error=exc)
            raise
        else:
            await self._fire_after_tool_call(call, hook_state, started, result=result)
            return result

    async def _invoke_normalized(self, tool_obj: Tool, call: ToolCall, context: ToolContext | None) -> Any:
        """Run the tool with errors normalized to BubError."""

        tool_name = tool_obj.name
        try:
            value = self._invoke_tool(
                tool_name=tool_name,
                tool_obj=tool_obj,
                tool_args=call.arguments,
                context=context,
            )
            if inspect.isawaitable(value):
                value = await value
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
        return value

    async def _apply_before_tool_call(
        self,
        call: ToolCall,
        hook_state: dict[str, Any],
        started: float,
    ) -> tuple[ToolCall, Callable[[], Any] | None]:
        """Run before_tool_call and translate deny/replace into a short-circuit thunk."""

        if self._hooks is None:
            return call, None
        call, decision = await self._hooks.before_tool_call(call, state=hook_state)
        if decision.action == "deny":
            error = BubError(
                ErrorKind.TOOL,
                decision.message or f"Tool '{call.tool}' call denied by policy hook.",
            )
            await self._fire_after_tool_call(call, hook_state, started, error=error)

            def raise_denied() -> Any:
                raise error

            return call, raise_denied
        if decision.action == "replace":
            await self._fire_after_tool_call(call, hook_state, started, result=decision.result)
            return call, lambda: decision.result
        return call, None

    async def _fire_after_tool_call(
        self,
        call: ToolCall,
        state: dict[str, Any],
        started: float,
        *,
        result: Any = None,
        error: Exception | None = None,
    ) -> None:
        if self._hooks is None:
            return
        duration_ms = int((time.monotonic() - started) * 1000)
        outcome = ToolCallResult(
            run_id=call.run_id,
            tool=call.tool,
            arguments=call.arguments,
            result=None if error is not None else result,
            error=error,
            duration_ms=duration_ms,
        )
        await self._hooks.after_tool_call(call, outcome, state=state)


# Central registry for tools. Tools defined with the @tool decorator are automatically added here.
REGISTRY: dict[str, Tool] = {}


def _add_logging(tool: Tool) -> Tool:
    handler = tool.handler

    async def wrapped(*args, **kwargs):
        call_kwargs = kwargs.copy()
        if tool.context:
            call_kwargs.pop("context", None)
        reporter = _TOOL_CALL_REPORTER.get()
        if reporter is None:
            _log_tool_call(tool.name, args, call_kwargs)
        else:
            reporter.start(tool.name, args, call_kwargs)
        start = time.monotonic()

        try:
            result = handler(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            elapsed_time = (time.monotonic() - start) * 1000
            if reporter is None:
                logger.exception("tool.call.error name={} elapsed_time={:.2f}ms", tool.name, elapsed_time)
            else:
                reporter.error(tool.name, exc, elapsed_time)
            raise
        else:
            elapsed_time = (time.monotonic() - start) * 1000
            if reporter is None:
                logger.info("tool.call.success name={} elapsed_time={:.2f}ms", tool.name, elapsed_time)
            else:
                reporter.success(tool.name, result, elapsed_time)
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
