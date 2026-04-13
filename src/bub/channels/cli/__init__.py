import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterable
from datetime import datetime
from hashlib import md5
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from republic import StreamEvent
from rich import get_console
from rich.live import Live

from bub.builtin.agent import Agent
from bub.builtin.tape import TapeInfo
from bub.channels.base import Channel
from bub.channels.cli.renderer import CliRenderer
from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.tools import REGISTRY
from bub.types import MessageHandler


class CliChannel(Channel):
    """A simple CLI channel for testing and debugging."""

    name = "cli"
    _stop_event: asyncio.Event

    def __init__(self, on_receive: MessageHandler, agent: Agent) -> None:
        self._on_receive = on_receive
        self._agent = agent
        self._message_template = {
            "chat_id": "cli_chat",
            "channel": self.name,
            "session_id": "cli_session",
        }
        self._mode = "agent"  # or "shell"
        self._main_task: asyncio.Task | None = None
        self._renderer = CliRenderer(get_console())
        self._last_tape_info: TapeInfo | None = None
        self._workspace = self._agent.framework.workspace
        self._prompt = self._build_prompt(self._workspace)

    async def _refresh_tape_info(self) -> None:
        tape = self._agent.tapes.session_tape(self._message_template["session_id"], self._workspace)
        info = await self._agent.tapes.info(tape.name)
        self._last_tape_info = info

    def set_metadata(self, session_id: str | None = None, chat_id: str | None = None) -> None:
        if session_id is not None:
            self._message_template["session_id"] = session_id
        if chat_id is not None:
            self._message_template["chat_id"] = chat_id

    async def start(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event
        self._main_task = asyncio.create_task(self._main_loop())

    async def stop(self) -> None:
        if self._main_task is not None:
            self._main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._main_task

    async def send(self, message: ChannelMessage) -> None:
        if message.kind != "error":
            return
        self._renderer.error(message.content)

    async def _main_loop(self) -> None:
        self._renderer.welcome(model=self._agent.settings.model, workspace=str(self._workspace))
        await self._refresh_tape_info()
        request_completed = asyncio.Event()

        while not self._stop_event.is_set():
            try:
                with patch_stdout(raw=True):
                    raw = (await self._prompt.prompt_async(self._prompt_message())).strip()
            except KeyboardInterrupt:
                self._renderer.info("Interrupted. Use ',quit' to exit.")
                continue
            except EOFError:
                break

            if not raw:
                continue
            if raw in {",quit", ",exit"}:
                break

            request = self._normalize_input(raw)

            message = ChannelMessage(
                session_id=self._message_template["session_id"],
                channel=self._message_template["channel"],
                chat_id=self._message_template["chat_id"],
                content=request,
                lifespan=self.message_lifespan(request_completed),
            )
            await self._on_receive(message)
            await request_completed.wait()
            request_completed.clear()

        self._renderer.info("Bye.")
        self._stop_event.set()

    @contextlib.asynccontextmanager
    async def message_lifespan(self, request_completed: asyncio.Event) -> AsyncGenerator[None, None]:
        try:
            yield
        finally:
            await self._refresh_tape_info()
            request_completed.set()

    def _normalize_input(self, raw: str) -> str:
        if self._mode != "shell":
            return raw
        if raw.startswith(","):
            return raw
        return f",{raw}"

    def _prompt_message(self) -> FormattedText:
        cwd = Path.cwd().name
        symbol = ">" if self._mode == "agent" else ","
        return FormattedText([("bold", f"{cwd} {symbol} ")])

    async def stream_events(
        self, message: ChannelMessage, stream: AsyncIterable[StreamEvent]
    ) -> AsyncIterable[StreamEvent]:
        live: Live | None = None
        text = ""
        try:
            async for event in stream:
                if event.kind == "text":
                    content = str(event.data.get("delta", ""))
                    if not content.strip() and not text:
                        continue  # skip leading whitespace-only events
                    if live is None:
                        live = self._renderer.start_stream(message.kind)
                    text += content
                    self._renderer.update_stream(live, kind=message.kind, text=text)
                yield event
        finally:
            if live is not None:
                self._renderer.finish_stream(live, kind=message.kind, text=text)

    def _build_prompt(self, workspace: Path) -> PromptSession[str]:
        kb = KeyBindings()

        @kb.add("c-x", eager=True)
        def _toggle_mode(event) -> None:
            self._mode = "shell" if self._mode == "agent" else "agent"
            event.app.invalidate()

        def _tool_sort_key(tool_name: str) -> tuple[str, str]:
            section, _, name = tool_name.rpartition(".")
            return (section, name)

        history_file = self._history_file(self._agent.settings.home, workspace)
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_file))
        tool_names = sorted((f",{name}" for name in REGISTRY), key=_tool_sort_key)
        completer = WordCompleter(tool_names, ignore_case=True)
        return PromptSession(
            completer=completer,
            complete_while_typing=True,
            key_bindings=kb,
            history=history,
            bottom_toolbar=self._render_bottom_toolbar,
        )

    def _render_bottom_toolbar(self) -> FormattedText:
        info = self._last_tape_info
        now = datetime.now().strftime("%H:%M")
        left = f"{now}  mode:{self._mode}"
        right = (
            f"model:{self._agent.settings.model}  "
            f"entries:{field_of(info, 'entries', '-')} "
            f"anchors:{field_of(info, 'anchors', '-')} "
            f"last:{field_of(info, 'last_anchor', None) or '-'}"
        )
        return FormattedText([("", f"{left}  {right}")])

    @staticmethod
    def _history_file(home: Path, workspace: Path) -> Path:
        workspace_hash = md5(str(workspace).encode("utf-8"), usedforsecurity=False).hexdigest()
        return home / "history" / f"{workspace_hash}.history"
