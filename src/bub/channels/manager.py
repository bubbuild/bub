import asyncio
import contextlib
import functools
from collections.abc import AsyncIterable, Collection

from loguru import logger
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub import config
from bub.channels.base import Channel, Interface, Lifecycle
from bub.channels.handler import BufferedMessageHandler
from bub.channels.message import ChannelMessage
from bub.configure import Settings, ensure_config
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.runtime import StreamEvent
from bub.turn_admission import AdmitDecision, SessionTurnController, TurnSnapshot
from bub.types import Envelope, MessageHandler, State
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
        self._session_controllers: dict[str, SessionTurnController] = {}
        self._session_handlers: dict[str, MessageHandler] = {}

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
        controller = self._session_controllers.get(session_id)
        if controller is None:
            logger.info(f"channel.manager quit session_id={session_id}, cancelled 0 tasks")
            return
        controller.clear_pending()
        tasks = set(controller.active_tasks)
        current_task = asyncio.current_task()
        cancelled_count = 0
        for task in tasks:
            if task is current_task:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            cancelled_count += 1
        controller.active_tasks.difference_update(task for task in tasks if task is not current_task)
        self._drop_empty_controller(session_id)
        logger.info(f"channel.manager quit session_id={session_id}, cancelled {cancelled_count} tasks")

    def enabled_channels(self) -> list[Channel]:
        """Channels are enabled by the following rules:
        - Interfaces are enabled only if explicitly *included*.
        - Lifecycles are always enabled unless explicitly *excluded*.
        - Regular channels depend on the values of include and exclude.
        """
        included_channels = [
            name.strip() for name in self._enabled_channels if name.strip() and not name.strip().startswith("!")
        ]
        excluded_channels = {name.strip()[1:] for name in self._enabled_channels if name.strip().startswith("!")}

        if "all" in included_channels:
            return [
                channel
                for channel in self._channels.values()
                if channel.name not in excluded_channels and channel.enabled and not isinstance(channel, Interface)
            ]
        channels = [
            channel
            for name, channel in self._channels.items()
            if name in included_channels and name not in excluded_channels and channel.enabled
        ]
        if not any(not isinstance(channel, Lifecycle) for channel in channels):
            return channels
        enabled_names = {channel.name for channel in channels}
        channels.extend(
            channel
            for channel in self._channels.values()
            if channel.name not in enabled_names
            and channel.name not in excluded_channels
            and channel.enabled
            and isinstance(channel, Lifecycle)
        )
        return channels

    def _controller(self, session_id: str) -> SessionTurnController:
        controller = self._session_controllers.get(session_id)
        if controller is None:
            controller = SessionTurnController(session_id, self.framework.get_steering_inbox())
            self._session_controllers[session_id] = controller
        return controller

    def _drop_empty_controller(self, session_id: str) -> None:
        controller = self._session_controllers.get(session_id)
        if controller is None:
            return
        if controller.active() or controller.pending_queue:
            return
        self._session_controllers.pop(session_id, None)

    def _on_task_done(self, session_id: str, task: asyncio.Task) -> None:
        if task.cancelled():
            logger.info("channel.manager task cancelled session_id={}", session_id)
        else:
            task.exception()  # to log any exception
        controller = self._session_controllers.get(session_id)
        if controller is None:
            return
        controller.active_tasks.discard(task)
        self._schedule_pending(session_id)
        self._drop_empty_controller(session_id)

    async def _admit_message(self, message: ChannelMessage) -> bool:
        try:
            session_id = await self._resolve_message_session(message)
        except Exception as exc:
            logger.exception("channel.manager resolve_session failed")
            await self.framework._hook_runtime.notify_error(stage="resolve_session", error=exc, message=message)
            return False
        controller = self._controller(session_id)
        state = self._admission_state(message, session_id)
        try:
            decision = await self.framework.admit_message(
                session_id=session_id,
                message=message,
                turn=controller.snapshot(state),
            )
        except Exception as exc:
            logger.exception("channel.manager admission hook failed")
            await self.framework._hook_runtime.notify_error(stage="admit_message", error=exc, message=message)
            return True
        if decision is None:
            self._drop_empty_controller(session_id)
            return True
        admitted = await self._apply_admission_decision(controller, message, decision, state)
        if not admitted or not controller.active():
            self._drop_empty_controller(session_id)
        return admitted

    async def _apply_admission_decision(
        self,
        controller: SessionTurnController,
        message: ChannelMessage,
        decision: AdmitDecision,
        state: State,
    ) -> bool:
        action = decision.action
        if action == "process":
            return True
        if action == "drop":
            logger.info(
                "channel.manager admission drop session_id={} reason={}",
                message.session_id,
                decision.reason,
            )
            return False
        if action == "follow_up":
            return self._queue_pending(controller, message, decision.reason)
        if action == "steer":
            if controller.active() and await self.framework.steer_message(
                message=message,
                session_id=controller.session_id,
                state=state,
                reason=decision.reason,
            ):
                return False
            return self._queue_pending(controller, message, decision.reason)
        logger.warning("channel.manager admission unknown action={} session_id={}", decision.action, message.session_id)
        return True

    def _queue_pending(
        self,
        controller: SessionTurnController,
        message: ChannelMessage,
        reason: str | None,
    ) -> bool:
        if not controller.active():
            return True
        controller.add_pending(message)
        logger.info(
            "channel.manager admission follow_up session_id={} pending_count={} reason={}",
            message.session_id,
            len(controller.pending_queue),
            reason,
        )
        return False

    def _schedule_message(self, message: ChannelMessage) -> asyncio.Task:
        controller = self._controller(message.session_id)
        task = asyncio.create_task(self._run_message(message))
        task.add_done_callback(functools.partial(self._on_task_done, message.session_id))
        controller.active_tasks.add(task)
        return task

    def _schedule_pending(self, session_id: str) -> None:
        controller = self._session_controllers.get(session_id)
        if controller is None or controller.active():
            return
        message = controller.pop_pending()
        if message is not None:
            self._schedule_message(message)

    async def _resolve_message_session(self, message: ChannelMessage) -> str:
        session_id = await self.framework.resolve_session(message)
        message.session_id = session_id
        return session_id

    @staticmethod
    def _admission_state(message: Envelope, session_id: str) -> State:
        state: State = {"session_id": session_id}
        context = field_of(message, "context", {})
        if isinstance(context, dict) and (thread_id := context.get("thread_id")):
            state["_runtime_thread_id"] = thread_id
        return state

    async def _run_message(self, message: ChannelMessage) -> None:
        result = await self.framework.process_inbound(message, self._stream_output)
        state = getattr(result, "state", {"session_id": message.session_id})
        await self._promote_steering_to_pending(message.session_id, state)

    async def _promote_steering_to_pending(self, session_id: str, state: State) -> None:
        steering_inbox = self.framework.get_steering_inbox()
        if steering_inbox is None:
            return
        messages = await steering_inbox.drain_messages(state)
        if not messages:
            return
        controller = self._controller(session_id)
        for message in messages:
            controller.add_pending(message)
        logger.info(
            "channel.manager queued remaining steering session_id={} pending_count={} steering_count={}",
            session_id,
            len(controller.pending_queue),
            len(messages),
        )

    async def listen_and_run(self) -> None:
        stop_event = asyncio.Event()
        self.framework.bind_outbound_router(self)
        async with self.framework.running():
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
            controller.clear_pending()
            for task in set(controller.active_tasks):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                count += 1
        self._session_controllers.clear()
        logger.info(f"channel.manager cancelled {count} in-flight tasks")
        for channel in self.enabled_channels():
            await channel.stop()

    async def admit_channel_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None:
        channel_name = field_of(message, "channel")
        if channel_name is None:
            return None
        channel = self.get_channel(str(channel_name))
        if channel is None:
            return None
        return await channel.admit_message(session_id=session_id, message=message, turn=turn)
