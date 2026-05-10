import asyncio
import contextlib
import functools
from collections.abc import AsyncIterable, Collection

from loguru import logger
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from republic import StreamEvent

from bub import config
from bub.channels.base import Channel
from bub.channels.handler import BufferedMessageHandler
from bub.channels.message import ChannelMessage
from bub.configure import Settings, ensure_config
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.turn_admission import AdmitAction, AdmitDecision, SessionTurnController
from bub.types import Envelope, MessageHandler
from bub.utils import wait_until_stopped


@config()
class ChannelSettings(Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_", extra="ignore", env_file=".env")

    enabled_channels: str = Field(
        default="all", description="Comma-separated list of enabled channels, or 'all' for all channels."
    )
    debounce_seconds: float = Field(
        default=1.0,
        description="Minimum seconds between processing two messages from the same channel to prevent overload.",
    )
    max_wait_seconds: float = Field(
        default=10.0,
        description="Maximum seconds to wait for processing before new messages reach the channel.",
    )
    active_time_window: float = Field(
        default=60.0,
        description="Time window in seconds to consider a channel active for processing messages.",
    )
    stream_output: bool = Field(default=False, description="Whether to stream model output to channels in real-time.")


class ChannelManager:
    def __init__(
        self,
        framework: BubFramework,
        enabled_channels: Collection[str] | None = None,
        stream_output: bool | None = None,
    ) -> None:
        self.framework = framework
        self._channels: dict[str, Channel] = self.framework.get_channels(self.on_receive)
        self._settings = ensure_config(ChannelSettings)
        self._stream_output = stream_output if stream_output is not None else self._settings.stream_output
        if enabled_channels is not None:
            self._enabled_channels = list(enabled_channels)
        else:
            self._enabled_channels = self._settings.enabled_channels.split(",")
        self._messages = asyncio.Queue[ChannelMessage]()
        self._session_handlers: dict[str, MessageHandler] = {}
        self._session_controllers: dict[str, SessionTurnController] = {}

    async def on_receive(self, message: ChannelMessage) -> None:
        channel = message.channel
        session_id = message.session_id
        if channel not in self._channels:
            logger.warning(f"Received message from unknown channel '{channel}', ignoring.")
            return
        if session_id not in self._session_handlers:
            handler: MessageHandler
            if self._channels[channel].needs_debounce:
                handler = BufferedMessageHandler(
                    self._messages.put,
                    active_time_window=self._settings.active_time_window,
                    max_wait_seconds=self._settings.max_wait_seconds,
                    debounce_seconds=self._settings.debounce_seconds,
                )
            else:
                handler = self._messages.put
            self._session_handlers[session_id] = handler
        await self._session_handlers[session_id](message)

    def get_channel(self, name: str) -> Channel | None:
        return self._channels.get(name)

    async def dispatch_output(self, message: Envelope) -> bool:
        channel_name = field_of(message, "output_channel", field_of(message, "channel"))
        if channel_name is None:
            return False

        channel_key = str(channel_name)
        channel = self.get_channel(channel_key)
        if channel is None:
            return False

        outbound = ChannelMessage(
            session_id=str(field_of(message, "session_id", f"{channel_key}:default")),
            channel=channel_key,
            chat_id=str(field_of(message, "chat_id", "default")),
            content=content_of(message),
            context=field_of(message, "context", {}),
            kind=field_of(message, "kind", "normal"),
        )
        await channel.send(outbound)
        return True

    def wrap_stream(self, message: Envelope, stream: AsyncIterable[StreamEvent]) -> AsyncIterable[StreamEvent]:
        channel_name = field_of(message, "output_channel", field_of(message, "channel"))
        if channel_name is None:
            return stream

        channel_key = str(channel_name)
        channel = self.get_channel(channel_key)
        if channel is None:
            return stream

        return channel.stream_events(message, stream)

    async def quit(self, session_id: str) -> None:
        cancelled = await self._cancel_tasks(session_id)
        logger.info(f"channel.manager quit session_id={session_id}, cancelled {cancelled} tasks")

    def enabled_channels(self) -> list[Channel]:
        if "all" in self._enabled_channels:
            # Exclude 'cli' channel from 'all' to prevent interference with other channels
            return [channel for name, channel in self._channels.items() if name != "cli" and channel.enabled]
        return [
            channel for name, channel in self._channels.items() if name in self._enabled_channels and channel.enabled
        ]

    def _controller(self, session_id: str) -> SessionTurnController:
        controller = self._session_controllers.get(session_id)
        if controller is None:
            controller = SessionTurnController(
                session_id=session_id,
                steering=self.framework.turn_control(session_id),
            )
            self._session_controllers[session_id] = controller
        return controller

    def _drop_empty_controller(self, session_id: str) -> None:
        controller = self._session_controllers.get(session_id)
        if controller is None:
            return
        if controller.active() or controller.pending_queue or controller.steering.has_messages():
            return
        self._session_controllers.pop(session_id, None)
        self.framework.clear_turn_control(session_id)

    def _on_task_done(self, session_id: str, task: asyncio.Task) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            task.exception()  # to log any exception
        controller = self._session_controllers.get(session_id)
        if controller is None:
            return
        controller.active_tasks.discard(task)
        if not controller.active():
            controller.promote_steering_to_pending()
        self._schedule_pending(session_id)
        self._drop_empty_controller(session_id)

    async def _cancel_tasks(self, session_id: str) -> int:
        controller = self._session_controllers.get(session_id)
        if controller is None:
            self.framework.clear_turn_control(session_id)
            return 0
        controller.closing = True
        controller.clear_pending()
        controller.steering.drain_injected()
        tasks = set(controller.active_tasks)
        self.framework.request_turn_cancel(session_id)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        controller.active_tasks.difference_update(tasks)
        self._drop_empty_controller(session_id)
        return len(tasks)

    async def _admit_message(self, message: ChannelMessage) -> bool:
        try:
            session_id = await self._resolve_message_session(message)
        except Exception as exc:
            logger.exception("channel.manager resolve_session failed")
            await self.framework._hook_runtime.notify_error(stage="resolve_session", error=exc, message=message)
            return False
        controller = self._controller(session_id)

        snapshot = controller.snapshot(supports_steering=self.framework.supports_steering())
        try:
            decision = await self.framework.admit_message(
                session_id=session_id,
                message=message,
                turn=snapshot,
            )
        except Exception as exc:
            logger.exception("channel.manager admission hook failed")
            await self.framework._hook_runtime.notify_error(stage="admit_message", error=exc, message=message)
            return True
        if decision is None:
            self._drop_empty_controller(session_id)
            return True
        admitted = await self._apply_admission_decision(controller, message, decision)
        if not admitted or not controller.active():
            self._drop_empty_controller(session_id)
        return admitted

    async def _apply_admission_decision(
        self,
        controller: SessionTurnController,
        message: ChannelMessage,
        decision: AdmitDecision,
    ) -> bool:
        action = _normalize_admit_action(decision.action)
        if action == AdmitAction.PROCESS:
            return True
        if action == AdmitAction.DROP:
            logger.info(
                "channel.manager admission drop session_id={} reason={}",
                message.session_id,
                decision.reason,
            )
            return False
        if action == AdmitAction.INJECT:
            if controller.active() and self.framework.supports_steering() and controller.steering.inject(message):
                logger.info(
                    "channel.manager admission inject session_id={} reason={}",
                    message.session_id,
                    decision.reason,
                )
                return False
            fallback = decision.fallback or AdmitAction.WAIT
            if fallback == AdmitAction.INJECT:
                fallback = AdmitAction.WAIT
            return await self._apply_admission_decision(
                controller,
                message,
                AdmitDecision(action=fallback, reason=decision.reason),
            )
        if action == AdmitAction.WAIT:
            if not controller.active():
                return True
            controller.add_pending(message)
            logger.info(
                "channel.manager admission wait session_id={} pending_count={} reason={}",
                message.session_id,
                len(controller.pending_queue),
                decision.reason,
            )
            return False
        if action == AdmitAction.CANCEL_AND_PROCESS:
            if not controller.active():
                return True
            controller.add_pending(message)
            self.framework.request_turn_cancel(message.session_id)
            logger.info(
                "channel.manager admission cancel_and_process session_id={} pending_count={} reason={}",
                message.session_id,
                len(controller.pending_queue),
                decision.reason,
            )
            return False
        logger.warning("channel.manager admission unknown action={} session_id={}", decision.action, message.session_id)
        return True

    def _schedule_message(self, message: ChannelMessage) -> asyncio.Task:
        controller = self._controller(message.session_id)
        if not controller.active():
            self.framework.reset_turn_cancel(message.session_id)
        task = asyncio.create_task(self._run_message(message))
        task.set_name(f"bub:{message.session_id}")
        task.add_done_callback(functools.partial(self._on_task_done, message.session_id))
        controller.active_tasks.add(task)
        return task

    def _schedule_pending(self, session_id: str) -> None:
        controller = self._session_controllers.get(session_id)
        if controller is None or controller.active() or controller.closing:
            return
        message = controller.pop_pending()
        if message is not None:
            self._schedule_message(message)

    async def _resolve_message_session(self, message: ChannelMessage) -> str:
        session_id = await self.framework.resolve_session(message)
        message.session_id = session_id
        setattr(message, "_runtime_session_id", session_id)  # noqa: B010
        return session_id

    async def _run_message(self, message: ChannelMessage) -> None:
        setattr(message, "_runtime_managed_turn", True)  # noqa: B010
        self.framework.turn_control(message.session_id)
        await self.framework.process_inbound(message, self._stream_output)

    async def listen_and_run(self) -> None:
        stop_event = asyncio.Event()
        self.framework.bind_outbound_router(self)
        for channel in self.enabled_channels():
            await channel.start(stop_event)
        logger.info("channel.manager started listening")
        try:
            while True:
                message = await wait_until_stopped(self._messages.get(), stop_event)
                if not await self._admit_message(message):
                    continue
                self._schedule_message(message)
        except asyncio.CancelledError:
            logger.info("channel.manager received shutdown signal")
        except Exception:
            logger.exception("channel.manager error")
            raise
        finally:
            self.framework.bind_outbound_router(None)
            await self.shutdown()
            logger.info("channel.manager stopped")

    async def shutdown(self) -> None:
        count = 0
        for controller in list(self._session_controllers.values()):
            controller.closing = True
            controller.clear_pending()
            controller.steering.drain_injected()
            for task in set(controller.active_tasks):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                count += 1
        self._session_controllers.clear()
        for session_id in list(self.framework._turn_controls):
            self.framework.clear_turn_control(session_id)
        logger.info(f"channel.manager cancelled {count} in-flight tasks")
        for channel in self.enabled_channels():
            await channel.stop()


def _normalize_admit_action(action: AdmitAction | str) -> AdmitAction | str:
    if isinstance(action, AdmitAction):
        return action
    try:
        return AdmitAction(action)
    except ValueError:
        return action
