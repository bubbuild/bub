"""Pluggy hook namespace and framework hook specifications."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pluggy
from republic import AsyncStreamEvents, AsyncTapeStore, TapeContext
from republic.tape import TapeStore

from bub.types import Envelope, MessageHandler, State

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
    def load_state(self, message: Envelope, session_id: str) -> State:
        """Load state snapshot for one session."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def build_prompt(self, message: Envelope, session_id: str, state: State) -> str | list[dict]:
        """Build model prompt for this turn.

        Returns either a plain text string or a list of content parts
        (OpenAI multimodal format) when media attachments are present.
        """
        raise NotImplementedError

    @hookspec(firstresult=True)
    def run_model(self, prompt: str | list[dict], session_id: str, state: State) -> str:
        """Run model for one turn and return plain text output. Should not be implemented if `run_model_stream` is implemented."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def run_model_stream(self, prompt: str | list[dict], session_id: str, state: State) -> AsyncStreamEvents:
        """Run model for one turn and return a stream of events. Should not be implemented if `run_model` is implemented."""
        raise NotImplementedError

    @hookspec
    def save_state(
        self,
        session_id: str,
        state: State,
        message: Envelope,
        model_output: str,
    ) -> None:
        """Persist state updates after one model turn."""

    @hookspec
    def render_outbound(
        self,
        message: Envelope,
        session_id: str,
        state: State,
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
    def on_error(self, stage: str, error: Exception, message: Envelope | None) -> None:
        """Observe framework errors from any stage."""

    @hookspec
    def system_prompt(self, prompt: str | list[dict], state: State) -> str:
        """Provide a system prompt to be prepended to all model prompts."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def provide_tape_store(self) -> TapeStore | AsyncTapeStore:
        """Provide a tape store instance for Bub's conversation recording feature."""
        ...

    @hookspec
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        """Provide a list of channels for receiving messages."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def build_tape_context(self) -> TapeContext:
        """Build a tape context for the current session, to be used to build context messages."""
        raise NotImplementedError
