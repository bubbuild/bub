import sys
from datetime import datetime
from pathlib import Path
from typing import cast

import typer
from loguru import logger

from bub import inquirer as bub_inquirer
from bub.builtin.agent import Agent
from bub.builtin.context import default_tape_context
from bub.builtin.settings import DEFAULT_MODEL, load_settings
from bub.builtin.steering import InMemorySteeringInbox
from bub.channels.base import Channel
from bub.channels.message import ChannelMessage, MediaItem
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.runtime import AsyncStreamEvents
from bub.runtime_options import RuntimeChoice, RuntimeOptions
from bub.tape import TapeContext, TapeStore
from bub.turn_admission import AdmitDecision, TurnSnapshot
from bub.types import Envelope, MessageHandler, State, SteeringInboxProtocol

AGENTS_FILE_NAME = "AGENTS.md"
MODEL_PROVIDER_CHOICES: tuple[str, ...] = (
    "openrouter",
    "openai",
    "anthropic",
    "gemini",
    "azure",
    "bedrock",
    "ollama",
    "groq",
    "mistral",
    "deepseek",
)
DEFAULT_SYSTEM_PROMPT = """\
<general_instruct>
Call tools or skills to finish the task.
</general_instruct>
<response_instruct>
Before ending this run, you MUST determine whether a response needs to be sent via channel, checking the following conditions:
1. Has the user asked you a question waiting for your answer?
2. Is there any error or important information that needs to be sent to the user immediately?
3. If it is a casual chat, does the conversation need to be continued?

**IMPORTANT:** Your plain/direct reply in this chat will be ignored.
**Therefore, you MUST send messages via channel using the correct skill if a response is needed.**

When responding to a channel message, you MUST:
1. Identify the channel from the message metadata (e.g., `$telegram`, `$discord`)
2. Send your message as instructed by the channel skill (e.g., `telegram` skill for `$telegram` channel)
</response_instruct>
<context_contract>
Excessively long context may cause model call failures. In this case, you MAY use tape.info to retrieve the token usage and you SHOULD use tape.handoff tool to shorten the retrieved history.
</context_contract>
"""


class BuiltinImpl:
    """Default hook implementations for basic runtime operations."""

    def __init__(self, framework: BubFramework) -> None:
        from bub.builtin import tools  # noqa: F401

        self.framework = framework
        self._agent: Agent | None = None

    def _get_agent(self) -> Agent:
        if self._agent is None:
            self._agent = Agent(self.framework)
        return self._agent

    async def _recover_session_model(self, session_id: str) -> str | None:
        """Recover the latest per-session model override recorded on the session tape.

        The ``model`` tool records each switch as a ``model_switch`` event on the
        session's tape. Scanning that tape here (before the per-turn fork exists)
        reads the persisted store, so a choice from a prior turn or restart is
        restored. Returns ``None`` when nothing was recorded, so a fresh session
        never inherits another session's model.
        """
        session = self._get_agent().tape.session_tape(session_id, self.framework.workspace)
        entries = list(await session.store.fetch_all(session.query().kinds("event")))
        for entry in reversed(entries):
            if entry.kind == "event" and entry.payload.get("name") == "model_switch":
                model = (entry.payload.get("data") or {}).get("model")
                return str(model) if model else None
        return None

    @staticmethod
    async def _discard_message(_: ChannelMessage) -> None:
        return

    @staticmethod
    def _split_model_identifier(model: str) -> tuple[str, str]:
        provider, separator, model_name = model.partition(":")
        if separator and provider and model_name:
            return provider.strip(), model_name.strip()
        default_provider, _, default_model_name = DEFAULT_MODEL.partition(":")
        fallback_model_name = model.strip() or default_model_name
        return default_provider, fallback_model_name

    @staticmethod
    def _provider_choices(current_provider: str) -> list[str]:
        choices = list(MODEL_PROVIDER_CHOICES)
        if current_provider and current_provider not in choices:
            choices.append(current_provider)
        choices.append("custom")
        return choices

    def _channel_choices(self) -> list[str]:
        return [c for c in self.framework.get_channels(self._discard_message) if c != "cli"]

    @staticmethod
    def _default_enabled_channels(current_value: object, available_channels: list[str]) -> list[str]:
        if isinstance(current_value, str) and current_value.strip() and current_value.strip().lower() != "all":
            selected = [name.strip() for name in current_value.split(",") if name.strip() in available_channels]
            return selected
        return available_channels

    @staticmethod
    def _configured_models() -> list[str]:
        settings = load_settings()
        models = [settings.model, *(settings.fallback_models or [])]
        return list(dict.fromkeys(model for model in models if model))

    @hookimpl
    def resolve_session(self, message: ChannelMessage) -> str:
        session_id = field_of(message, "session_id")
        if session_id is not None and str(session_id).strip():
            return str(session_id)
        channel = str(field_of(message, "channel", "default"))
        chat_id = str(field_of(message, "chat_id", "default"))
        return f"{channel}:{chat_id}"

    @hookimpl
    async def load_state(self, message: ChannelMessage, session_id: str) -> State:
        lifespan = field_of(message, "lifespan")
        if lifespan is not None:
            await lifespan.__aenter__()
        state = {"session_id": session_id, "_runtime_agent": self._get_agent()}
        if context := field_of(message, "context_str"):
            state["context"] = context
        # Carry over a previously recorded per-session model override from the
        # session tape. Only set when a prior turn actually recorded one, so a
        # fresh/unknown session never inherits another session's model.
        if model := await self._recover_session_model(session_id):
            state["model"] = model
        if model := field_of(message, "context", {}).get("model"):
            state["model"] = model
        if thread_id := field_of(message, "context", {}).get("thread_id"):
            state["_runtime_thread_id"] = thread_id
        return state

    @hookimpl
    async def save_state(self, session_id: str, state: State, message: ChannelMessage, model_output: str) -> None:
        tp, value, traceback = sys.exc_info()
        lifespan = field_of(message, "lifespan")
        if lifespan is not None:
            await lifespan.__aexit__(tp, value, traceback)
        # The per-session model override is persisted on the session tape by the
        # ``model`` tool itself (a ``model_switch`` event, merged back at end of
        # turn), so nothing to write here — this hook only closes the lifespan.

    @hookimpl
    async def build_prompt(self, message: ChannelMessage, session_id: str, state: State) -> str | list[dict]:
        content = content_of(message)
        if content.startswith(","):
            message.kind = "command"
            return content
        context = field_of(message, "context_str")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        context_prefix = f"{context}\n---Date: {now}---\n" if context else ""
        text = f"{context_prefix}{content}"

        media = field_of(message, "media") or []
        if not media:
            return text

        media_parts: list[dict] = []
        for item in cast("list[MediaItem]", media):
            match item.type:
                case "image":
                    data_url = await item.get_url()
                    if not data_url:
                        continue
                    media_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                case _:
                    pass  # TODO: Not supported for now
        if media_parts:
            return [{"type": "text", "text": text}, *media_parts]
        return text

    @hookimpl
    async def run_model_stream(self, prompt: str | list[dict], session_id: str, state: State) -> AsyncStreamEvents:
        return await self._get_agent().run_stream(
            session_id=session_id,
            prompt=prompt,
            state=state,
            model=state.get("model"),
        )

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        from bub.builtin import cli

        app.command("run")(cli.run)
        app.command("chat")(cli.chat)
        app.command("onboard")(cli.onboard)
        app.command("hooks", hidden=True)(cli.list_hooks)
        app.command("gateway")(cli.gateway)
        app.command("install")(cli.install)
        app.command("uninstall")(cli.uninstall)
        app.command("update")(cli.update)

    @hookimpl
    def onboard_config(self, current_config: dict[str, object]) -> dict[str, object] | None:
        current_model = current_config.get("model")
        model_default = str(current_model) if isinstance(current_model, str) and current_model else DEFAULT_MODEL
        provider_default, model_name_default = self._split_model_identifier(model_default)

        provider = bub_inquirer.ask_fuzzy(
            "LLM provider",
            choices=self._provider_choices(provider_default),
            default=provider_default,
        )
        if provider == "custom":
            provider = bub_inquirer.ask_text("Custom provider", default=provider_default) or provider_default

        model_name = bub_inquirer.ask_text("LLM model", default=model_name_default)
        if not model_name:
            model_name = model_name_default
        model = f"{provider}:{model_name}"

        api_key = bub_inquirer.ask_secret("API key (optional)")

        current_api_base = current_config.get("api_base")
        api_base_default = str(current_api_base) if isinstance(current_api_base, str) else ""
        api_base = bub_inquirer.ask_text("API base (optional)", default=api_base_default)

        available_channels = self._channel_choices()
        default_channels = self._default_enabled_channels(current_config.get("enabled_channels"), available_channels)
        enabled_channels = bub_inquirer.ask_checkbox(
            "Channels",
            choices=available_channels,
            enabled=default_channels,
            validate=lambda values: True if values else "Select at least one channel.",
        )

        stream_output = bub_inquirer.ask_confirm("Stream output", default=bool(current_config.get("stream_output")))
        config: dict[str, object] = {
            "model": model,
            "enabled_channels": ",".join(enabled_channels),
            "stream_output": stream_output,
        }
        if api_key:
            config["api_key"] = api_key
        if api_base:
            config["api_base"] = api_base
        return config

    @hookimpl
    def provide_runtime_options(
        self,
        session_id: str,
        workspace: Path | None = None,
    ) -> RuntimeOptions | None:
        del session_id, workspace
        models = self._configured_models()
        if not models:
            return None

        return RuntimeOptions(
            models=[RuntimeChoice(id=model, name=model) for model in models],
            current_model=models[0],
        )

    def _read_agents_file(self, state: State) -> str:
        workspace = state.get("_runtime_workspace", str(Path.cwd()))
        prompt_path = Path(workspace) / AGENTS_FILE_NAME
        if not prompt_path.is_file():
            return ""
        try:
            return prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @hookimpl
    def system_prompt(self, prompt: str | list[dict], state: State) -> str:
        # Read the content of AGENTS.md under workspace
        return DEFAULT_SYSTEM_PROMPT + "\n\n" + self._read_agents_file(state)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        from bub.channels.cli import CliChannel
        from bub.channels.telegram import TelegramChannel

        return [
            TelegramChannel(on_receive=message_handler),
            CliChannel(on_receive=message_handler, agent=self._get_agent()),
        ]

    @hookimpl
    async def on_error(self, stage: str, error: Exception, message: Envelope | None) -> None:
        if message is not None:
            outbound = ChannelMessage(
                session_id=field_of(message, "session_id", "unknown"),
                channel=field_of(message, "channel", "default"),
                chat_id=field_of(message, "chat_id", "default"),
                content=f"An error occurred at stage '{stage}': {error}",
                kind="error",
            )
            await self.framework._hook_runtime.call_many("dispatch_outbound", message=outbound)

    @hookimpl
    async def dispatch_outbound(self, message: Envelope) -> bool:
        content = content_of(message)
        session_id = field_of(message, "session_id")
        if field_of(message, "output_channel") != "cli":
            logger.info("session.run.outbound session_id={} content={}", session_id, content)
        return await self.framework.dispatch_via_router(message)

    @hookimpl
    def render_outbound(
        self,
        message: Envelope,
        session_id: str,
        state: State,
        model_output: str,
    ) -> list[ChannelMessage]:
        outbound = ChannelMessage(
            session_id=session_id,
            channel=field_of(message, "channel", "default"),
            chat_id=field_of(message, "chat_id", "default"),
            content=model_output,
            output_channel=field_of(message, "output_channel", "default"),
            kind=field_of(message, "kind", "normal"),
        )
        return [outbound]

    @hookimpl
    def provide_tape_store(self) -> TapeStore:
        import bub
        from bub.builtin.store import FileTapeStore

        return FileTapeStore(directory=bub.home / "tapes")

    @hookimpl
    def build_tape_context(self) -> TapeContext:
        return default_tape_context()

    @hookimpl
    def provide_steering_inbox(self) -> SteeringInboxProtocol:
        return InMemorySteeringInbox()

    @hookimpl
    async def admit_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None:
        outbound_router = self.framework._outbound_router
        if outbound_router is None:
            return None
        return await outbound_router.admit_channel_message(session_id=session_id, message=message, turn=turn)
