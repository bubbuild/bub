"""Hook-first Bub framework runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pluggy
import typer
from dotenv import load_dotenv
from loguru import logger
from republic import AsyncTapeStore, TapeContext
from republic.tape import TapeStore

from bub.envelope import content_of, field_of, unpack_batch
from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs
from bub.types import Envelope, MessageHandler, OutboundChannelRouter, TurnResult

if TYPE_CHECKING:
    from bub.channels.base import Channel


load_dotenv()


@dataclass(frozen=True)
class PluginStatus:
    is_success: bool
    detail: str | None = None


class BubFramework:
    """Minimal framework core. Everything grows from hook skills."""

    def __init__(self) -> None:
        self.workspace = Path.cwd().resolve()
        self._plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
        self._plugin_manager.add_hookspecs(BubHookSpecs)
        self._hook_runtime = HookRuntime(self._plugin_manager)
        self._plugin_status: dict[str, PluginStatus] = {}
        self._outbound_router: OutboundChannelRouter | None = None

    def _load_builtin_hooks(self) -> None:
        from bub.builtin.hook_impl import BuiltinImpl

        impl = BuiltinImpl(self)

        try:
            self._plugin_manager.register(impl, name="builtin")
        except Exception as exc:
            self._plugin_status["builtin"] = PluginStatus(is_success=False, detail=str(exc))
        else:
            self._plugin_status["builtin"] = PluginStatus(is_success=True)

    def load_hooks(self) -> None:
        import importlib.metadata

        self._load_builtin_hooks()
        for entry_point in importlib.metadata.entry_points(group="bub"):
            try:
                plugin = entry_point.load()
                if callable(plugin):  # Support entry points that are classes
                    plugin = plugin(self)
                self._plugin_manager.register(plugin, name=entry_point.name)
            except Exception as exc:
                logger.warning(f"Failed to load plugin '{entry_point.name}': {exc}")
                self._plugin_status[entry_point.name] = PluginStatus(is_success=False, detail=str(exc))
            else:
                self._plugin_status[entry_point.name] = PluginStatus(is_success=True)

    def create_cli_app(self) -> typer.Typer:
        """Create CLI app by collecting commands from hooks. Can be used for custom CLI entry point."""
        app = typer.Typer(name="bub", help="Batteries-included, hook-first AI framework", add_completion=False)

        @app.callback(invoke_without_command=True)
        def _main(
            ctx: typer.Context,
            workspace: str | None = typer.Option(None, "--workspace", "-w", help="Path to the workspace"),
        ) -> None:
            if workspace:
                self.workspace = Path(workspace).resolve()
            ctx.obj = self

        self._hook_runtime.call_many_sync("register_cli_commands", app=app)
        return app

    async def process_inbound(self, inbound: Envelope) -> TurnResult:
        """Run one inbound message through hooks and return turn result."""

        try:
            session_id = await self._hook_runtime.call_first(
                "resolve_session", message=inbound
            ) or self._default_session_id(inbound)
            if isinstance(inbound, dict):
                inbound.setdefault("session_id", session_id)
            state = {"_runtime_workspace": str(self.workspace)}
            for hook_state in reversed(
                await self._hook_runtime.call_many("load_state", message=inbound, session_id=session_id)
            ):
                if isinstance(hook_state, dict):
                    state.update(hook_state)
            prompt = await self._hook_runtime.call_first(
                "build_prompt", message=inbound, session_id=session_id, state=state
            )
            if not prompt:
                prompt = content_of(inbound)
            model_output = ""
            try:
                model_output = await self._hook_runtime.call_first(
                    "run_model", prompt=prompt, session_id=session_id, state=state
                )
                if model_output is None:
                    await self._hook_runtime.notify_error(
                        stage="run_model:fallback",
                        error=RuntimeError("no model skill returned output"),
                        message=inbound,
                    )
                    model_output = prompt if isinstance(prompt, str) else content_of(inbound)
                else:
                    model_output = str(model_output)
            finally:
                await self._hook_runtime.call_many(
                    "save_state",
                    session_id=session_id,
                    state=state,
                    message=inbound,
                    model_output=model_output,
                )

            outbounds = await self._collect_outbounds(inbound, session_id, state, model_output)
            for outbound in outbounds:
                await self._hook_runtime.call_many("dispatch_outbound", message=outbound)
            return TurnResult(session_id=session_id, prompt=prompt, model_output=model_output, outbounds=outbounds)
        except Exception as exc:
            logger.exception("Error processing inbound message")
            await self._hook_runtime.notify_error(stage="turn", error=exc, message=inbound)
            raise

    def hook_report(self) -> dict[str, list[str]]:
        """Return hook implementation summary for diagnostics."""

        return self._hook_runtime.hook_report()

    def bind_outbound_router(self, router: OutboundChannelRouter | None) -> None:
        self._outbound_router = router

    async def dispatch_via_router(self, message: Envelope) -> bool:
        if self._outbound_router is None:
            return False
        return await self._outbound_router.dispatch(message)

    async def quit_via_router(self, session_id: str) -> None:
        if self._outbound_router is not None:
            await self._outbound_router.quit(session_id)

    @staticmethod
    def _default_session_id(message: Envelope) -> str:
        session_id = field_of(message, "session_id")
        if session_id is not None:
            return str(session_id)
        channel = str(field_of(message, "channel", "default"))
        chat_id = str(field_of(message, "chat_id", "default"))
        return f"{channel}:{chat_id}"

    async def _collect_outbounds(
        self,
        message: Envelope,
        session_id: str,
        state: dict[str, Any],
        model_output: str,
    ) -> list[Envelope]:
        batches = await self._hook_runtime.call_many(
            "render_outbound",
            message=message,
            session_id=session_id,
            state=state,
            model_output=model_output,
        )
        outbounds: list[Envelope] = []
        for batch in batches:
            outbounds.extend(unpack_batch(batch))
        if outbounds:
            return outbounds

        fallback: dict[str, Any] = {
            "content": model_output,
            "session_id": session_id,
        }
        channel = field_of(message, "channel")
        chat_id = field_of(message, "chat_id")
        if channel is not None:
            fallback["channel"] = channel
        if chat_id is not None:
            fallback["chat_id"] = chat_id
        return [fallback]

    def get_channels(self, message_handler: MessageHandler) -> dict[str, Channel]:
        channels: dict[str, Channel] = {}
        for result in self._hook_runtime.call_many_sync("provide_channels", message_handler=message_handler):
            for channel in result:
                if channel.name not in channels:
                    channels[channel.name] = channel
        return channels

    def get_tape_store(self) -> TapeStore | AsyncTapeStore | None:
        return self._hook_runtime.call_first_sync("provide_tape_store")

    def get_system_prompt(self, prompt: str | list[dict], state: dict[str, Any]) -> str:
        return "\n\n".join(
            result
            for result in reversed(self._hook_runtime.call_many_sync("system_prompt", prompt=prompt, state=state))
            if result
        )

    def build_tape_context(self) -> TapeContext:
        return self._hook_runtime.call_first_sync("build_tape_context")
