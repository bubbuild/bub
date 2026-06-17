import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from datetime import datetime
from hashlib import md5
from pathlib import Path
from time import monotonic
from typing import Any

from loguru import logger
from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich import get_console
from rich.spinner import SPINNERS
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
from bub.tools import REGISTRY, tool_call_reporter
from bub.turn_admission import AdmitDecision, TurnSnapshot
from bub.types import Envelope, MessageHandler

_GENERATION_SPINNER: str = SPINNERS["dots"]["frames"]  # type: ignore[assignment]
_PROMPT_REFRESH_INTERVAL: float = SPINNERS["dots"]["interval"] / 1000.0  # type: ignore[operator]


class _StreamPrinter:
    def __init__(self, *, console, print_head: Callable[[], None], expand_thinking: bool) -> None:
        self._console = console
        self._print_head = print_head
        self._expand_thinking = expand_thinking
        self._reasoning_chars = 0
        self._reasoning_streaming = False
        self.head_printed = False

    async def render(self, event: StreamEvent) -> bool:
        if event.kind == "reasoning":
            await self._record_reasoning(str(event.data.get("delta", "")))
            return True

        if event.kind == "text":
            return await self._print_content(str(event.data.get("delta", "")))
        elif event.kind == "tool_call":
            await self._print_stream_boundary()
        elif event.kind == "final":
            await self._print_end()
        return True

    async def _record_reasoning(self, reasoning: str) -> None:
        if not self._expand_thinking:
            if self._reasoning_chars == 0:
                await self._ensure_head()
            self._reasoning_chars += len(reasoning)
            return

        await self._ensure_head()
        if not self._reasoning_streaming:
            await self._print(Text("[-] Thinking", style="dim"))
            self._reasoning_streaming = True
        await self._print(Text(reasoning, style="dim"), end="", highlight=False)

    async def _print_content(self, content: str) -> bool:
        if not (content.strip() or self.head_printed or self._reasoning_chars or self._reasoning_streaming):
            return False
        await self._ensure_head()
        await self._close_reasoning_stream()
        await self._flush_reasoning()
        await self._print(content, end="", highlight=False)
        return True

    async def _print_end(self) -> None:
        if self._reasoning_chars:
            await self._ensure_head()
        await self._flush_reasoning()
        if self.head_printed:
            await self._print("")

    async def _print_stream_boundary(self) -> None:
        await self._close_reasoning_stream()
        await self._flush_reasoning()
        if self.head_printed:
            await self._print("")

    async def _ensure_head(self) -> None:
        if self.head_printed:
            return
        await run_in_terminal(self._print_head, render_cli_done=False)
        self.head_printed = True

    async def _close_reasoning_stream(self) -> None:
        if not self._reasoning_streaming:
            return
        await self._print("")
        self._reasoning_streaming = False

    async def _flush_reasoning(self) -> None:
        if self._reasoning_chars <= 0:
            return
        label = Text(f"[+] Thinking ({self._reasoning_chars} chars hidden)", style="dim")
        await self._print(Tree(label, guide_style="dim", expanded=False))
        self._reasoning_chars = 0

    async def _print(self, *args: Any, **kwargs: Any) -> None:
        await run_in_terminal(lambda: self._console.print(*args, **kwargs), render_cli_done=False)


class _CliToolCallReporter:
    def __init__(self, renderer: CliRenderer) -> None:
        self._renderer = renderer

    def start(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self._renderer.tool_call_start(name=name, args=args, kwargs=kwargs)

    def success(self, name: str, result: object, elapsed_ms: float) -> None:
        self._renderer.tool_call_success(name=name, result=result, elapsed_ms=elapsed_ms)

    def error(self, name: str, error: BaseException, elapsed_ms: float) -> None:
        self._renderer.tool_call_error(name=name, error=error, elapsed_ms=elapsed_ms)


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
        self._llm_loop_running = False
        self._main_task: asyncio.Task | None = None
        self._renderer = CliRenderer(get_console())
        self._last_tape_info: TapeInfo | None = None
        self._workspace = self._agent.framework.workspace
        self._prompt = self._build_prompt(self._workspace)

    def _suppress_logs(self) -> None:
        with contextlib.suppress(ValueError):
            logger.remove()

    async def _refresh_tape_info(self) -> None:
        tape = self._agent.tape.session_tape(self._message_template["session_id"], self._workspace)
        info = await tape.info()
        self._last_tape_info = info

    def set_metadata(self, session_id: str | None = None, chat_id: str | None = None) -> None:
        if session_id is not None:
            self._message_template["session_id"] = session_id
        if chat_id is not None:
            self._message_template["chat_id"] = chat_id

    async def start(self, stop_event: asyncio.Event) -> None:
        self._suppress_logs()
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

        while not self._stop_event.is_set():
            try:
                with patch_stdout(raw=True):
                    raw = (
                        await self._prompt.prompt_async(
                            self._prompt_message,
                            refresh_interval=_PROMPT_REFRESH_INTERVAL,
                        )
                    ).strip()
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
                self._renderer.input_echo(self._prompt_label(), raw)
                self._toggle_thinking()
                continue

            request = self._normalize_input(raw)
            self._renderer.input_echo(self._prompt_label(), raw)

            message = ChannelMessage(
                session_id=self._message_template["session_id"],
                channel=self._message_template["channel"],
                chat_id=self._message_template["chat_id"],
                content=request,
                lifespan=self.message_lifespan(),
            )
            self._set_llm_loop_running(True)
            try:
                await self._on_receive(message)
            except Exception:
                self._set_llm_loop_running(False)
                raise

        self._renderer.info("Bye.")
        self._stop_event.set()

    @contextlib.asynccontextmanager
    async def message_lifespan(self) -> AsyncGenerator[None, None]:
        self._set_llm_loop_running(True)
        try:
            yield
        finally:
            await self._refresh_tape_info()
            self._set_llm_loop_running(False)

    def _normalize_input(self, raw: str) -> str:
        if self._mode != "shell":
            return raw
        if raw.startswith(","):
            return raw
        return f",{raw}"

    def _prompt_message(self) -> FormattedText:
        prompt = self._prompt_label()
        if not self._llm_loop_running:
            return FormattedText([("bold", prompt)])
        index = int(monotonic() / _PROMPT_REFRESH_INTERVAL) % len(_GENERATION_SPINNER)
        spinner = _GENERATION_SPINNER[index]
        return FormattedText([
            ("blue", f"\n{spinner} Generating\n"),
            ("bold", prompt),
        ])

    def _prompt_label(self) -> str:
        cwd = Path.cwd().name
        symbol = ">" if self._mode == "agent" else ","
        return f"{cwd} {symbol} "

    async def stream_events(
        self, message: ChannelMessage, stream: AsyncIterable[StreamEvent]
    ) -> AsyncIterable[StreamEvent]:
        console = get_console()
        printer = _StreamPrinter(
            console=console,
            print_head=lambda: self._renderer.print_head(message.kind),
            expand_thinking=self._expand_thinking,
        )
        with tool_call_reporter(_CliToolCallReporter(self._renderer)):
            async for event in stream:
                if await printer.render(event):
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
            erase_when_done=True,
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

    def _invalidate_prompt(self) -> None:
        with contextlib.suppress(Exception):
            self._prompt.app.invalidate()

    def _set_llm_loop_running(self, running: bool) -> None:
        if self._llm_loop_running == running:
            return
        self._llm_loop_running = running
        self._invalidate_prompt()

    @staticmethod
    def _history_file(home: Path, workspace: Path) -> Path:
        workspace_hash = md5(str(workspace).encode("utf-8"), usedforsecurity=False).hexdigest()
        return home / "history" / f"{workspace_hash}.history"

    def admit_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None:
        if not turn.is_running:
            return None
        return AdmitDecision("follow_up", reason="cli session is already generating")
