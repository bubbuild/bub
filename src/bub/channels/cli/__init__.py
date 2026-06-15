import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from datetime import datetime
from hashlib import md5
from pathlib import Path

from loguru import logger
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich import get_console
from rich.status import Status
from rich.text import Text
from rich.tree import Tree

import bub
from bub.builtin.agent import Agent
from bub.builtin.tape import TapeInfo
from bub.channels.base import Interface
from bub.channels.cli.renderer import CliRenderer
from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.runtime import StreamEvent
from bub.tools import REGISTRY
from bub.types import MessageHandler


class _StreamPrinter:
    def __init__(self, *, console, print_head: Callable[[], None], expand_thinking: bool) -> None:
        self._console = console
        self._print_head = print_head
        self._expand_thinking = expand_thinking
        self._reasoning_chars = 0
        self._reasoning_streaming = False
        self._reasoning_status: Status | None = None
        self.head_printed = False

    def render(self, event: StreamEvent) -> bool:
        if event.kind == "reasoning":
            self._record_reasoning(str(event.data.get("delta", "")))
            return True

        if event.kind == "text":
            return self._print_content(str(event.data.get("delta", "")))
        elif event.kind == "tool_call":
            self._print_stream_boundary()
        elif event.kind == "final":
            self._print_end()
        return True

    def _record_reasoning(self, reasoning: str) -> None:
        if not self._expand_thinking:
            if self._reasoning_chars == 0:
                self._ensure_head()
                self._start_reasoning_status()
            self._reasoning_chars += len(reasoning)
            return

        self._ensure_head()
        if not self._reasoning_streaming:
            self._console.print(Text("[-] Thinking", style="dim"))
            self._reasoning_streaming = True
        self._console.print(Text(reasoning, style="dim"), end="", highlight=False)

    def _print_content(self, content: str) -> bool:
        if not (content.strip() or self.head_printed or self._reasoning_chars or self._reasoning_streaming):
            return False
        self._ensure_head()
        self._close_reasoning_stream()
        self._flush_reasoning()
        self._console.print(content, end="", highlight=False)
        return True

    def _print_end(self) -> None:
        if self._reasoning_chars:
            self._ensure_head()
        self._flush_reasoning()
        if self.head_printed:
            self._console.print("")

    def _print_stream_boundary(self) -> None:
        self._close_reasoning_stream()
        self._flush_reasoning()
        if self.head_printed:
            self._console.print("")

    def _ensure_head(self) -> None:
        if self.head_printed:
            return
        self._print_head()
        self.head_printed = True

    def _close_reasoning_stream(self) -> None:
        if not self._reasoning_streaming:
            return
        self._console.print("")
        self._reasoning_streaming = False

    def _flush_reasoning(self) -> None:
        if self._reasoning_chars <= 0:
            return
        self._stop_reasoning_status()
        label = Text(f"[+] Thinking ({self._reasoning_chars} chars hidden)", style="dim")
        self._console.print(Tree(label, guide_style="dim", expanded=False))
        self._reasoning_chars = 0

    def _start_reasoning_status(self) -> None:
        if self._reasoning_status is not None:
            return
        self._reasoning_status = self._console.status(Text("Thinking", style="dim"), spinner_style="dim")
        self._reasoning_status.start()

    def _stop_reasoning_status(self) -> None:
        if self._reasoning_status is None:
            return
        self._reasoning_status.stop()
        self._reasoning_status = None


class CliChannel(Interface):
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
        self._expand_thinking = False
        self._main_task: asyncio.Task | None = None
        self._renderer = CliRenderer(get_console())
        self._last_tape_info: TapeInfo | None = None
        self._workspace = self._agent.framework.workspace
        self._prompt = self._build_prompt(self._workspace)

    def _install_log_sink(self) -> int:
        with contextlib.suppress(ValueError):
            logger.remove()
        return logger.add(self._renderer.log, colorize=False, format="{level:<8} | {message}")

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
        self._log_handler_id = self._install_log_sink()
        self._stop_event = stop_event
        self._main_task = asyncio.create_task(self._main_loop())

    async def stop(self) -> None:
        if self._main_task is not None:
            self._main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._main_task
        with contextlib.suppress(ValueError):
            logger.remove(self._log_handler_id)

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
            if raw == ",thinking":
                self._toggle_thinking()
                continue

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
        console = get_console()
        printer = _StreamPrinter(
            console=console,
            print_head=lambda: self._renderer.print_head(message.kind),
            expand_thinking=self._expand_thinking,
        )
        async for event in stream:
            if printer.render(event):
                yield event

    def _build_prompt(self, workspace: Path) -> PromptSession[str]:
        kb = KeyBindings()

        @kb.add("c-x", eager=True)
        def _toggle_mode(event) -> None:
            self._mode = "shell" if self._mode == "agent" else "agent"
            event.app.invalidate()

        def _tool_sort_key(tool_name: str) -> tuple[str, str]:
            section, _, name = tool_name.rpartition(".")
            return (section, name)

        history_file = self._history_file(bub.home, workspace)
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_file))
        tool_names = sorted([*(f",{name}" for name in REGISTRY), ",thinking"], key=_tool_sort_key)
        completer = WordCompleter(tool_names, ignore_case=True, sentence=True)
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
            f"thinking:{'expand' if self._expand_thinking else 'collapse'}  "
            f"model:{self._agent.settings.model}  "
            f"entries:{field_of(info, 'entries', '-')} "
            f"anchors:{field_of(info, 'anchors', '-')} "
            f"last:{field_of(info, 'last_anchor', None) or '-'}"
        )
        return FormattedText([("", f"{left}  {right}")])

    def _toggle_thinking(self) -> None:
        self._expand_thinking = not self._expand_thinking
        state = "expanded" if self._expand_thinking else "collapsed"
        self._renderer.info(f"Thinking output is now {state}.")

    @staticmethod
    def _history_file(home: Path, workspace: Path) -> Path:
        workspace_hash = md5(str(workspace).encode("utf-8"), usedforsecurity=False).hexdigest()
        return home / "history" / f"{workspace_hash}.history"
