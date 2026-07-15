"""Pluggy namespace and extension specifications for Bub hooks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pluggy

from bub.channels.admission import AdmitDecision, SteeringInbox, TurnSnapshot
from bub.channels.contracts import MessageHandler
from bub.envelope import Envelope
from bub.hooks.interception import (
    LlmCallDecision,
    LlmCallRequest,
    LlmCallResult,
    ToolCall,
    ToolCallDecision,
    ToolCallResult,
)
from bub.model_selection import ModelOptions
from bub.streaming import AsyncStreamEvents
from bub.tape import AsyncTapeStore, TapeContext, TapeStore
from bub.turn import TurnState

if TYPE_CHECKING:
    from bub.channels.base import Channel

BUB_HOOK_NAMESPACE = "bub"
hookspec = pluggy.HookspecMarker(BUB_HOOK_NAMESPACE)
hookimpl = pluggy.HookimplMarker(BUB_HOOK_NAMESPACE)


class BubHookSpecs:
    """Hook contract for Bub framework extensions."""

    @hookspec(firstresult=True)
    def resolve_session(self, message: Envelope) -> str:
        """Resolve session id for one inbound message."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def build_prompt(self, message: Envelope, session_id: str, state: TurnState) -> str | list[dict]:
        """Build model prompt for this turn.

        Returns either a plain text string or a list of content parts
        (OpenAI multimodal format) when media attachments are present.
        """
        raise NotImplementedError

    @hookspec(firstresult=True)
    def run_model(self, prompt: str | list[dict], session_id: str, state: TurnState) -> str:
        """Run model for one turn and return plain text output. Should not be implemented if `run_model_stream` is implemented."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def run_model_stream(self, prompt: str | list[dict], session_id: str, state: TurnState) -> AsyncStreamEvents:
        """Run model for one turn and return a stream of events. Should not be implemented if `run_model` is implemented.

        Implementations may honor a runtime model override by reading
        ``state["model"]`` (any ``provider:model`` string). The value takes
        effect on the turn in which it is read, so a model switched mid-turn via
        the `,model <id>` command applies from the *next* turn.
        """
        raise NotImplementedError

    @hookspec
    def load_state(self, message: Envelope, session_id: str) -> TurnState:
        """Load state snapshot for one session."""
        raise NotImplementedError

    @hookspec
    def save_state(
        self,
        session_id: str,
        state: TurnState,
        message: Envelope,
        model_output: str,
    ) -> None:
        """Persist state updates after one model turn."""

    @hookspec
    def render_outbound(
        self,
        message: Envelope,
        session_id: str,
        state: TurnState,
        model_output: str,
    ) -> list[Envelope]:
        """Render outbound messages from model output."""
        raise NotImplementedError

    @hookspec
    def dispatch_outbound(self, message: Envelope) -> bool:
        """Dispatch one outbound message to external channel(s)."""
        raise NotImplementedError

    @hookspec
    def register_cli_commands(self, app: Any) -> None:
        """Register CLI commands onto the root Typer application."""

    @hookspec
    def onboard_config(self, current_config: dict[str, Any]) -> dict[str, Any] | None:
        """Collect a plugin config fragment for the interactive onboarding command."""

    @hookspec
    def provide_model_options(
        self,
        session_id: str,
        workspace: Path | None,
    ) -> ModelOptions | None:
        """Provide model choices for a session."""

    @hookspec
    def on_error(self, stage: str, error: Exception, message: Envelope | None) -> None:
        """Observe framework errors from any stage."""

    @hookspec
    def before_llm_call(self, request: LlmCallRequest, state: TurnState) -> LlmCallRequest | LlmCallDecision | None:
        """Observe, modify or short-circuit an outgoing agent-loop LLM request.

        Implementations are chained in pluggy's LIFO order (last registered
        runs first): each receives the
        request as modified by earlier implementations, and may return a
        modified copy (``dataclasses.replace``), ``None`` to leave it
        unchanged, or ``LlmCallDecision.finish(text)`` to skip the provider
        call and emit ``text`` as the final output (cost guards / call
        limits). Exceptions are logged and skipped, never fatal.
        """

    @hookspec
    def after_llm_call(self, request: LlmCallRequest, result: LlmCallResult, state: TurnState) -> None:
        """Observe the terminal outcome of one agent-loop LLM call.

        Fires exactly once per completed call — success (for streaming
        completions, after the stream is fully consumed) or ``Exception``
        failure (``result.error`` set). Cancellation and consumer close
        (``BaseException``) intentionally bypass this hook. Return values
        are ignored; exceptions are logged and skipped.
        """

    @hookspec
    def before_tool_call(self, call: ToolCall, state: TurnState) -> ToolCallDecision | None:
        """Decide whether/how one tool invocation runs.

        Return ``None`` or ``ToolCallDecision.proceed(...)`` to continue
        (optionally with modified arguments, visible to later
        implementations), ``ToolCallDecision.replace(result)`` to skip the
        tool and use ``result``, or ``ToolCallDecision.deny(message)`` to
        surface a tool error instead. ``replace``/``deny`` short-circuit
        remaining implementations. Blocking is only possible via the
        decision object — exceptions are logged and skipped.
        """

    @hookspec
    def after_tool_call(self, call: ToolCall, result: ToolCallResult, state: TurnState) -> None:
        """Observe the terminal outcome of one tool invocation.

        Fires for success, failure (``result.error`` set), denial and
        replacement. Return values are ignored; exceptions are logged.
        """

    @hookspec
    def system_prompt(self, prompt: str | list[dict], state: TurnState) -> str:
        """Provide a system prompt to be prepended to all model prompts."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def provide_tape_store(self) -> TapeStore | AsyncTapeStore | None:
        """Provide a tape store instance for Bub's conversation recording feature."""
        raise NotImplementedError

    @hookspec
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        """Provide a list of channels for receiving messages."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def build_tape_context(self) -> TapeContext:
        """Build a tape context for the current session, to be used to build context messages."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def admit_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None:
        """Decide how to handle an inbound channel message for a session.

        Return ``None`` to keep Bub's default concurrent scheduling behavior.
        """
        raise NotImplementedError

    @hookspec(firstresult=True)
    def provide_steering_inbox(self) -> SteeringInbox | None:
        """Provide a steering inbox for the current session, to be used to queue and drain messages."""
        raise NotImplementedError
