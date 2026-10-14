"""Events emitted by the agent loop, their payloads, and the bus delivering them to handlers."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

import republic

from bub.errors import BubError
from bub.prompt import UserContent
from bub.turn import TurnState

if TYPE_CHECKING:
    from bub.core.tape import Tape
    from bub.core.tools import ToolContext


@dataclass(frozen=True)
class LlmCallRequest:
    """Outgoing agent-loop LLM request exposed to interception hooks.

    Hooks may return a modified copy (``dataclasses.replace``) to change the
    model or messages (``republic.Message`` objects) for this call. Tool objects are not exposed;
    ``tool_names`` is observational and altering the toolset is out of scope.
    """

    run_id: str
    model: str
    messages: list[republic.Message]
    tool_names: tuple[str, ...] = ()
    max_tokens: int | None = None


@dataclass(frozen=True)
class LlmCallResult:
    """Terminal outcome of one LLM call exposed to interception hooks.

    For streaming completions this is the fully accumulated final state, not
    a per-chunk view. ``error`` is the original raised exception (and other
    fields are best effort) when the call failed. Cancellation and consumer
    close are not observed.
    """

    run_id: str
    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    error: Exception | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class LlmCallDecision:
    """Short-circuit verdict returned by a ``before_llm_call`` hook."""

    action: Literal["finish"] = "finish"
    text: str = ""

    @classmethod
    def finish(cls, text: str) -> LlmCallDecision:
        """Skip the provider call and emit ``text`` as its final output."""

        return cls(action="finish", text=text)


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation exposed to interception hooks.

    ``context`` is the ``ToolContext`` the call runs with, when one is available; for
    example, ``context.code_mode`` marks calls made from model-written code (``run_code``).
    """

    run_id: str
    tool: str
    arguments: dict[str, Any]
    context: ToolContext | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ToolCallDecision:
    """Verdict returned by a ``before_tool_call`` hook.

    ``proceed`` continues with optional argument changes. ``replace`` skips
    the handler and uses the supplied result. ``deny`` skips the handler and
    surfaces the supplied message as a tool error.
    """

    action: Literal["proceed", "replace", "deny"] = "proceed"
    arguments: dict[str, Any] | None = None
    result: Any = None
    message: str | None = None

    @classmethod
    def proceed(cls, arguments: dict[str, Any] | None = None) -> ToolCallDecision:
        return cls(action="proceed", arguments=arguments)

    @classmethod
    def replace(cls, result: Any) -> ToolCallDecision:
        return cls(action="replace", result=result)

    @classmethod
    def deny(cls, message: str) -> ToolCallDecision:
        return cls(action="deny", message=message)


@dataclass
class ToolCallResult:
    """Terminal outcome exposed to hooks; model-facing results may be replaced in place."""

    run_id: str
    tool: str
    arguments: dict[str, Any]
    result: Any = None
    error: Exception | None = None
    duration_ms: int = 0


# Events. Each carries the turn ``state``; loop events also carry the run's ``tape``. ``Before*``
# events are mutable: handlers may replace the request or call, or set a decision, which stops delivery.


@dataclass(kw_only=True)
class AgentEvent:
    """Base class of agent-loop events; a handler for it receives every event."""

    state: TurnState

    @property
    def stopped(self) -> bool:
        """Whether delivery to the remaining handlers stops."""
        return False


@dataclass(kw_only=True)
class TapeEvent(AgentEvent):
    """An event of a run, carrying the run's tape (whose ``context.state`` is ``state``)."""

    tape: Tape


@dataclass(kw_only=True)
class RunStarted(TapeEvent):
    """A run began; ``metadata`` is caller-supplied context recorded with it."""

    model: str
    prompt: list[UserContent]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class StepStarted(TapeEvent):
    """A model step began; ``prompt`` is None when the step continues the conversation."""

    step: int
    prompt: list[UserContent] | None


@dataclass(kw_only=True)
class StepFinished(TapeEvent):
    """A model step ended with ``status`` ``ok``, ``continue``, ``error`` or ``auto_handoff``."""

    step: int
    status: str
    elapsed_ms: int
    error: str | None = None


@dataclass(kw_only=True)
class CommandFinished(TapeEvent):
    """A command prompt ran (see ``Agent.run_command``), with ``status`` ``ok`` or ``error``.

    ``line`` is the command without its prefix, ``name`` the command name, and ``output``
    the rendered result or the error message.
    """

    line: str
    name: str
    status: str
    elapsed_ms: int
    output: str


@dataclass(kw_only=True)
class BeforeLlmCall(TapeEvent):
    """An LLM call is about to be sent.

    Handlers may replace ``request`` (``dataclasses.replace``) or set ``decision`` to
    finish without calling the model; setting a decision stops delivery.
    """

    request: LlmCallRequest
    decision: LlmCallDecision | None = None

    @property
    def stopped(self) -> bool:
        return self.decision is not None


@dataclass(kw_only=True)
class AfterLlmCall(TapeEvent):
    """An LLM call completed or failed with an ``Exception``; cancellation is not observed."""

    request: LlmCallRequest
    result: LlmCallResult


@dataclass(kw_only=True)
class BeforeToolCall(AgentEvent):
    """A tool call is about to run.

    Handlers may replace ``call`` to change its arguments, or set ``decision`` to
    ``ToolCallDecision.replace(result)`` or ``deny(message)``, which stops delivery.
    """

    call: ToolCall
    decision: ToolCallDecision = field(default_factory=lambda: ToolCallDecision.proceed())

    @property
    def stopped(self) -> bool:
        return self.decision.action != "proceed"


@dataclass(kw_only=True)
class AfterToolCall(AgentEvent):
    """A tool call ended (success, failure, denial or replacement); assign ``result.result`` to change it."""

    call: ToolCall
    result: ToolCallResult


@dataclass(kw_only=True)
class ChatCompleted(TapeEvent):
    """One model step's exchange is complete and ready to be recorded.

    Also emitted when the step fails before calling the model (``context_error``) or a
    ``before_llm_call`` decision finishes it.
    """

    run_id: str
    model: str
    system_prompt: str | None
    new_messages: list[republic.Message]
    response_text: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[Any] | None = None
    usage: dict[str, Any] | None = None
    provider: str | None = None
    assistant_fields: dict[str, Any] = field(default_factory=dict)
    error: BubError | None = None
    context_error: BubError | None = None


E = TypeVar("E", bound=AgentEvent)
EventHandler = Callable[[E], object]
"""A sync or async function of an event; its result is awaited if awaitable, then ignored."""


class EventBus:
    """Deliver agent events to handlers registered by event type, in registration order.

    A handler registered for a base class receives its subclasses. Handler exceptions
    propagate to the agent loop.
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[type[AgentEvent], EventHandler[Any]]] = []

    @overload
    def on(self, event_type: type[E]) -> Callable[[EventHandler[E]], EventHandler[E]]: ...

    @overload
    def on(self, event_type: type[E], handler: EventHandler[E]) -> EventHandler[E]: ...

    def on(self, event_type: type[E], handler: EventHandler[E] | None = None) -> Any:
        """Register ``handler`` for ``event_type`` and return it.

        Without ``handler``, return a decorator registering the decorated function::

            @events.on(BeforeToolCall)
            def guard(event: BeforeToolCall) -> None: ...
        """
        if handler is None:
            return lambda handler: self.on(event_type, handler)
        self._handlers.append((event_type, handler))
        return handler

    def off(self, event_type: type[E], handler: EventHandler[E]) -> None:
        """Remove a handler registered for ``event_type``; do nothing if it is not registered."""
        with suppress(ValueError):
            self._handlers.remove((event_type, handler))

    async def emit(self, event: E) -> E:
        """Deliver ``event`` and return it, as modified by the handlers."""
        for event_type, handler in list(self._handlers):
            if event.stopped:
                break
            if isinstance(event, event_type):
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
        return event
