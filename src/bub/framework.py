"""Hook-first Bub framework runtime."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pluggy
import typer
from dotenv import load_dotenv
from loguru import logger

from bub import configure
from bub.channels.admission import AdmitDecision, SteeringInbox, TurnSnapshot
from bub.channels.contracts import ChannelRouter, MessageHandler
from bub.envelope import Envelope, content_of, field_of, unpack_batch
from bub.errors import BubError, ErrorKind
from bub.hooks.interception import AgentHooks
from bub.hooks.runtime import _SKIP_VALUE, HookRuntime
from bub.hooks.specs import BUB_HOOK_NAMESPACE, BubHookSpecs
from bub.model_selection import ModelOptions
from bub.tape import AsyncTapeStore, TapeContext, TapeStore
from bub.turn import TurnResult, TurnState
from bub.utils import maybe_context_manager

if TYPE_CHECKING:
    from bub.channels.base import Channel


load_dotenv()
DEFAULT_HOME = Path.home() / ".bub"
DEFAULT_CONFIG_FILE = (DEFAULT_HOME / "config.yml").resolve()


@dataclass(frozen=True)
class PluginStatus:
    is_success: bool
    detail: str | None = None


class BubFramework:
    """Minimal framework core. Everything grows from hook skills."""

    def __init__(self, config_file: Path = DEFAULT_CONFIG_FILE) -> None:
        self.workspace = Path.cwd().resolve()
        self.config_file = config_file.resolve()
        self._plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
        self._plugin_manager.add_hookspecs(BubHookSpecs)
        self._hook_runtime = HookRuntime(self._plugin_manager)
        self._agent_hooks = AgentHooks(self._hook_runtime)
        self._plugin_status: dict[str, PluginStatus] = {}
        self._channel_router: ChannelRouter | None = None
        self._tape_store: TapeStore | AsyncTapeStore | None = None
        self._steering_inbox: SteeringInbox | None = None
        configure.load(self.config_file)

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

        pending_plugins: list[tuple[str, Any]] = []

        self._load_builtin_hooks()
        for entry_point in importlib.metadata.entry_points(group="bub"):
            try:
                plugin = entry_point.load()
            except Exception as exc:
                logger.warning(f"Failed to load plugin '{entry_point.name}': {exc}")
                self._plugin_status[entry_point.name] = PluginStatus(is_success=False, detail=str(exc))
            else:
                pending_plugins.append((entry_point.name, plugin))

        for plugin_name, plugin in pending_plugins:
            try:
                if callable(plugin):  # Support entry points that are classes
                    plugin = plugin(self)
                self._plugin_manager.register(plugin, name=plugin_name)
            except Exception as exc:
                logger.warning(f"Failed to initialize plugin '{plugin_name}': {exc}")
                self._plugin_status[plugin_name] = PluginStatus(is_success=False, detail=str(exc))
            else:
                self._plugin_status[plugin_name] = PluginStatus(is_success=True)

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

    async def build_prompt(
        self, message: Envelope, session_id: str, state: dict[str, Any]
    ) -> str | list[dict[str, Any]]:
        """Build prompt for one message turn."""
        prompt = await self._hook_runtime.call_first(
            "build_prompt", message=message, session_id=session_id, state=state
        )
        if not prompt:
            prompt = content_of(message)
        return cast("str | list[dict[str, Any]]", prompt)

    async def build_state(self, message: Envelope, session_id: str) -> TurnState:
        state = {"_runtime_workspace": str(self.workspace), "_runtime_steering_inbox": self.get_steering_inbox()}
        for hook_state in reversed(
            await self._hook_runtime.call_many("load_state", message=message, session_id=session_id)
        ):
            if isinstance(hook_state, dict):
                state.update(hook_state)
        return state

    async def process_inbound(self, inbound: Envelope, stream_output: bool = False) -> TurnResult:
        """Run one inbound message through hooks and return turn result."""

        try:
            session_id = await self.resolve_session(inbound)
            if isinstance(inbound, dict):
                inbound.setdefault("session_id", session_id)
            state = await self.build_state(inbound, session_id)
            prompt = await self.build_prompt(inbound, session_id, state)
            model_output = ""
            try:
                model_output = await self._run_model(inbound, prompt, session_id, state, stream_output)
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
            return TurnResult(
                session_id=session_id,
                prompt=prompt,
                model_output=model_output,
                outbounds=outbounds,
                state=state,
            )
        except Exception as exc:
            logger.exception("Error processing inbound message")
            await self._hook_runtime.notify_error(stage="turn", error=exc, message=inbound)
            raise

    async def resolve_session(self, message: Envelope) -> str:
        """Resolve the canonical session id for a message."""

        resolved = await self._hook_runtime.call_first("resolve_session", message=message)
        return str(resolved or self._default_session_id(message))

    async def _run_model(
        self,
        inbound: Envelope,
        prompt: str | list[dict],
        session_id: str,
        state: dict[str, Any],
        stream_output: bool,
    ) -> str:
        if not stream_output:
            output = await self._hook_runtime.run_model(prompt=prompt, session_id=session_id, state=state)
            if output is None:
                await self._hook_runtime.notify_error(
                    stage="run_model",
                    error=RuntimeError("no model skill returned output"),
                    message=inbound,
                )
                return prompt if isinstance(prompt, str) else content_of(inbound)
            return output
        stream = await self._hook_runtime.run_model_stream(prompt=prompt, session_id=session_id, state=state)
        if stream is None:
            await self._hook_runtime.notify_error(
                stage="run_model",
                error=RuntimeError("no model skill returned output"),
                message=inbound,
            )
            return prompt if isinstance(prompt, str) else content_of(inbound)
        else:
            parts: list[str] = []
            events = self._channel_router.wrap_stream(inbound, stream) if self._channel_router is not None else stream
            async for event in events:
                if event.kind == "text":
                    parts.append(str(event.data.get("delta", "")))
                elif event.kind == "error":
                    # Turn "kind" to enum type otherwise BubError's __str__ won't work well.
                    data = {
                        **event.data,
                        "kind": ErrorKind(event.data.get("kind", "unknown")),
                    }
                    await self._hook_runtime.notify_error(stage="run_model", error=BubError(**data), message=inbound)
            return "".join(parts)

    def hook_report(self) -> dict[str, list[str]]:
        """Return hook implementation summary for diagnostics."""

        return self._hook_runtime.hook_report()

    def bind_channel_router(self, router: ChannelRouter | None) -> None:
        self._channel_router = router

    async def dispatch_via_channel_router(self, message: Envelope) -> bool:
        if self._channel_router is None:
            return False
        return await self._channel_router.dispatch_output(message)

    async def quit_via_channel_router(self, session_id: str) -> None:
        if self._channel_router is not None:
            await self._channel_router.quit(session_id)

    async def admit_message(self, *, session_id: str, message: Envelope, turn: TurnSnapshot) -> AdmitDecision | None:
        decision = await self._hook_runtime.call_first(
            "admit_message",
            session_id=session_id,
            message=message,
            turn=turn,
        )
        if decision is None or isinstance(decision, AdmitDecision):
            return decision
        raise TypeError("hook.admit_message must return AdmitDecision or None")

    async def get_model_options(
        self,
        *,
        session_id: str,
        workspace: str | Path | None = None,
    ) -> ModelOptions:
        """Collect model choices for one session."""

        resolved_workspace = self._resolve_workspace(workspace)
        results = await self._hook_runtime.call_many(
            "provide_model_options",
            session_id=session_id,
            workspace=resolved_workspace,
        )

        merged = ModelOptions()
        for result in results:
            if result is None:
                continue
            if not isinstance(result, ModelOptions):
                raise TypeError("hook.provide_model_options must return ModelOptions or None")
            merged = ModelOptions(
                models=[*merged.models, *result.models],
                current_model=merged.current_model or result.current_model,
            )
        return merged

    def _resolve_workspace(self, workspace: str | Path | None) -> Path:
        if workspace is None:
            return self.workspace
        return Path(workspace).expanduser().resolve()

    async def steer_message(
        self,
        *,
        message: Envelope,
        session_id: str,
        state: TurnState,
        reason: str | None = None,
    ) -> bool:
        inbox = self.get_steering_inbox()
        if inbox is None:
            return False
        state.setdefault("session_id", session_id)
        if reason is not None:
            with contextlib.suppress(AttributeError):
                message.context = {**field_of(message, "context", {}), "steering_reason": reason}
        await inbox.enqueue_message(message, state)
        return True

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

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncGenerator[contextlib.AsyncExitStack, None]:
        async with contextlib.AsyncExitStack() as stack:
            tape_store = self._hook_runtime.call_first_sync("provide_tape_store")
            # Allow plugins to return either TapeStore/AsyncTapeStore instances or context managers for them
            # This benefits plugins that need to initialize and clean up resources with the tape store.
            self._tape_store = await maybe_context_manager(tape_store, stack)

            steering_inbox = self._hook_runtime.call_first_sync("provide_steering_inbox")
            self._steering_inbox = await maybe_context_manager(steering_inbox, stack)
            try:
                yield stack
            finally:
                self._tape_store = None
                self._steering_inbox = None

    def get_tape_store(self) -> TapeStore | AsyncTapeStore | None:
        return self._tape_store

    def get_steering_inbox(self) -> SteeringInbox | None:
        return self._steering_inbox

    def get_agent_hooks(self) -> AgentHooks:
        return self._agent_hooks

    def get_system_prompt(self, prompt: str | list[dict], state: dict[str, Any]) -> str:
        return "\n\n".join(
            result
            for result in reversed(self._hook_runtime.call_many_sync("system_prompt", prompt=prompt, state=state))
            if result
        )

    def build_tape_context(self) -> TapeContext:
        context = self._hook_runtime.call_first_sync("build_tape_context")
        if isinstance(context, TapeContext):
            return context
        raise TypeError("hook.build_tape_context must return TapeContext")

    def collect_onboard_config(self) -> dict[str, Any]:
        current_config: dict[str, Any] = {}

        for impl in reversed(list(self._hook_runtime._iter_hookimpls("onboard_config"))):
            result = self._hook_runtime._invoke_impl_sync(
                hook_name="onboard_config",
                impl=impl,
                call_kwargs={"current_config": current_config},
                kwargs={"current_config": current_config},
            )
            if result is _SKIP_VALUE:
                continue
            if result is None:
                continue
            if not isinstance(result, dict):
                raise TypeError("hook.onboard_config must return dict or None")
            configure.merge(current_config, result)
        return configure.validate(current_config)
