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
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, AnyFormattedText, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, FormattedTextControl, HSplit, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from rich import get_console
from rich.spinner import SPINNERS
from rich.text import Text
from rich.tree import Tree

import bub
from bub.builtin.agent import Agent
from bub.channels.admission import AdmitDecision, TurnSnapshot
from bub.channels.base import Interface
from bub.channels.cli.ansi_bridge import render_to_ansi
from bub.channels.cli.renderer import CliRenderer
from bub.channels.cli.terminal_output import (
    TerminalPresenter,
    create_synchronized_output,
)
from bub.channels.cli.writers import MarkdownWriter
from bub.channels.contracts import MessageHandler
from bub.channels.message import ChannelMessage
from bub.envelope import Envelope, field_of
from bub.streaming import StreamEvent
from bub.tape import TapeInfo
from bub.tools import REGISTRY, tool_call_reporter

_GENERATION_SPINNER: str = SPINNERS["dots"]["frames"]  # type: ignore[assignment]
_PROMPT_REFRESH_INTERVAL: float = SPINNERS["dots"]["interval"] / 1000.0  # type: ignore[operator]


class _StreamPrinter:
    def __init__(
        self,
        *,
        console,
        print_head: Callable[[], None],
        expand_thinking: bool,
        presenter: TerminalPresenter,
        writer: MarkdownWriter | None = None,
        invalidate: Callable[[], None] | None = None,
    ) -> None:
        self._console = console
        self._print_head = print_head
        self._expand_thinking = expand_thinking
        self._presenter = presenter
        self._reasoning_chars = 0
        self._reasoning_streaming = False
        self._writer = writer or MarkdownWriter()
        self._invalidate = invalidate or (lambda: None)
        self._ansi_cache: tuple[int, str] | None = None
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
            await self.finish()
        return True

    async def _record_reasoning(self, reasoning: str) -> None:
        if not self._expand_thinking:
            if self._reasoning_chars == 0:
                await self._ensure_head()
            self._reasoning_chars += len(reasoning)
            self._invalidate()
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
        await self._write_text(content)
        return True

    async def finish(self) -> None:
        await self._close_reasoning_stream()
        if self._reasoning_chars:
            await self._ensure_head()
        await self._flush_reasoning()
        if self._writer.has_content():
            await self._flush_text()

    async def _print_stream_boundary(self) -> None:
        await self.finish()
        if self.head_printed:
            await self._print("")

    async def _ensure_head(self) -> None:
        if self.head_printed:
            return
        await self._presenter.write(self._print_head)
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

    async def _write_text(self, text: str) -> None:
        self._writer.append(text)
        self._ansi_cache = None
        self._invalidate()

    def render_live_ansi(self, *, width: int) -> str:
        if not self._writer.has_content():
            return ""
        if self._ansi_cache is None or self._ansi_cache[0] != width:
            rendered = render_to_ansi(self._writer.render_live(), width=width).rstrip("\n")
            self._ansi_cache = (width, rendered)
        return self._ansi_cache[1]

    def live_cursor_position(self, *, width: int) -> Point:
        rendered = self.render_live_ansi(width=width)
        return Point(x=0, y=max(0, len(rendered.splitlines()) - 1))

    def has_live_content(self) -> bool:
        return self._writer.has_content()

    async def _flush_text(self) -> None:
        finished = self._writer.render_final()
        if finished is None:
            return

        def commit() -> None:
            self._console.print(finished)
            self._writer.clear()

        await self._presenter.write(commit)
        self._ansi_cache = None
        self._invalidate()

    async def _print(self, *args: Any, **kwargs: Any) -> None:
        await self._presenter.write(lambda: self._console.print(*args, **kwargs))


class _CliToolCallReporter:
    def __init__(self, renderer: CliRenderer, presenter: TerminalPresenter) -> None:
        self._renderer = renderer
        self._presenter = presenter

    async def start(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        await self._presenter.write(lambda: self._renderer.tool_call_start(name=name, args=args, kwargs=kwargs))

    async def success(self, name: str, result: object, elapsed_ms: float) -> None:
        await self._presenter.write(
            lambda: self._renderer.tool_call_success(name=name, result=result, elapsed_ms=elapsed_ms)
        )

    async def error(self, name: str, error: BaseException, elapsed_ms: float) -> None:
        await self._presenter.write(
            lambda: self._renderer.tool_call_error(name=name, error=error, elapsed_ms=elapsed_ms)
        )


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
        self._generation_tick: asyncio.TimerHandle | None = None
        self._main_task: asyncio.Task | None = None
        self._stream_printer: _StreamPrinter | None = None
        self._renderer = CliRenderer(get_console())
        self._presenter = TerminalPresenter()
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
        self._stop_generation_animation()
        if self._main_task is not None:
            self._main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._main_task

    async def send(self, message: ChannelMessage) -> None:
        if message.kind != "error":
            return
        await self._presenter.write(lambda: self._renderer.error(message.content))

    async def _main_loop(self) -> None:
        await self._presenter.write(
            lambda: self._renderer.welcome(model=self._agent.settings.model, workspace=str(self._workspace))
        )
        await self._refresh_tape_info()

        while not self._stop_event.is_set():
            try:
                with patch_stdout(raw=True):
                    raw = (await self._prompt.prompt_async(self._prompt_message)).strip()
            except KeyboardInterrupt:
                await self._presenter.write(lambda: self._renderer.info("Interrupted. Use ',quit' to exit."))
                continue
            except EOFError:
                break

            if not raw:
                continue
            if raw in {",quit", ",exit"}:
                break
            if raw == ",thinking":
                await self._echo_input(raw)
                await self._toggle_thinking()
                continue

            request = self._normalize_input(raw)

            message = ChannelMessage(
                session_id=self._message_template["session_id"],
                channel=self._message_template["channel"],
                chat_id=self._message_template["chat_id"],
                context={"thread_id": self._message_template["session_id"]},  # use the same thread_id for all messages
                content=request,
                lifespan=self.message_lifespan(),
            )
            self._set_llm_loop_running(True)
            try:
                await self._on_receive(message)
            except Exception:
                self._set_llm_loop_running(False)
                raise

        self._stop_generation_animation()
        await self._presenter.write(lambda: self._renderer.info("Bye."))
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

    def _prompt_message(self) -> AnyFormattedText:
        return FormattedText([("bold", self._prompt_label())])

    def _live_output_message(self) -> AnyFormattedText:
        stream_printer: _StreamPrinter | None = getattr(self, "_stream_printer", None)
        if stream_printer is None:
            return FormattedText([])
        return ANSI(stream_printer.render_live_ansi(width=get_console().width))

    def _live_output_cursor(self) -> Point:
        stream_printer: _StreamPrinter | None = getattr(self, "_stream_printer", None)
        if stream_printer is None:
            return Point(x=0, y=0)
        return stream_printer.live_cursor_position(width=get_console().width)

    def _has_live_output(self) -> bool:
        stream_printer: _StreamPrinter | None = getattr(self, "_stream_printer", None)
        return stream_printer is not None and stream_printer.has_live_content()

    def _is_generating(self) -> bool:
        return getattr(self, "_stream_printer", None) is not None or self._llm_loop_running

    def _generation_status(self) -> FormattedText:
        index = int(monotonic() / _PROMPT_REFRESH_INTERVAL) % len(_GENERATION_SPINNER)
        spinner = _GENERATION_SPINNER[index]
        return FormattedText([("blue", f"{spinner} Generating")])

    def _prompt_label(self) -> str:
        cwd = Path.cwd().name
        symbol = ">" if self._mode == "agent" else ","
        return f"{cwd} {symbol} "

    async def _echo_input(self, raw: str, steering: bool = False) -> None:
        await self._presenter.write(lambda: self._renderer.input_echo(self._prompt_label(), raw, steering=steering))

    async def stream_events(
        self, message: ChannelMessage, stream: AsyncIterable[StreamEvent]
    ) -> AsyncIterable[StreamEvent]:
        console = get_console()
        printer = _StreamPrinter(
            console=console,
            print_head=lambda: self._renderer.print_head(message.kind),
            expand_thinking=self._expand_thinking,
            presenter=self._presenter,
            invalidate=self._invalidate_prompt,
        )
        self._stream_printer = printer
        self._invalidate_prompt()
        try:
            with tool_call_reporter(_CliToolCallReporter(self._renderer, self._presenter)):
                async for event in stream:
                    if await printer.render(event):
                        yield event
        finally:
            try:
                await printer.finish()
            finally:
                if self._stream_printer is printer:
                    self._stream_printer = None
                    self._invalidate_prompt()

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
        prompt: PromptSession[str] = PromptSession(
            completer=completer,
            complete_while_typing=True,
            key_bindings=kb,
            history=history,
            bottom_toolbar=self._render_bottom_toolbar,
            erase_when_done=True,
            output=create_synchronized_output(),
        )
        self._attach_live_layout(prompt)
        prompt.app.min_redraw_interval = _PROMPT_REFRESH_INTERVAL
        return prompt

    def _attach_live_layout(self, prompt: PromptSession[str]) -> None:
        root = prompt.layout.container
        if not isinstance(root, HSplit):
            raise TypeError("PromptSession root layout must be an HSplit")
        live_output = Window(
            FormattedTextControl(
                self._live_output_message,
                show_cursor=False,
                get_cursor_position=self._live_output_cursor,
            ),
            height=Dimension(min=0, weight=1),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        generation_status = Window(
            FormattedTextControl(self._generation_status),
            height=1,
            dont_extend_height=True,
        )
        root.children[0:0] = [
            ConditionalContainer(live_output, Condition(self._has_live_output)),
            ConditionalContainer(generation_status, Condition(self._is_generating)),
        ]

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

    async def _toggle_thinking(self) -> None:
        self._expand_thinking = not self._expand_thinking
        state = "expanded" if self._expand_thinking else "collapsed"
        await self._presenter.write(lambda: self._renderer.info(f"Thinking output is now {state}."))

    def _invalidate_prompt(self) -> None:
        with contextlib.suppress(Exception):
            self._prompt.app.invalidate()

    def _set_llm_loop_running(self, running: bool) -> None:
        if self._llm_loop_running == running:
            return
        self._llm_loop_running = running
        if running:
            self._schedule_generation_tick()
        else:
            self._stop_generation_animation()
        self._invalidate_prompt()

    def _schedule_generation_tick(self) -> None:
        if getattr(self, "_generation_tick", None) is not None:
            return
        self._generation_tick = asyncio.get_running_loop().call_later(
            _PROMPT_REFRESH_INTERVAL,
            self._tick_generation_status,
        )

    def _tick_generation_status(self) -> None:
        self._generation_tick = None
        if not self._llm_loop_running:
            return
        self._invalidate_prompt()
        self._schedule_generation_tick()

    def _stop_generation_animation(self) -> None:
        tick: asyncio.TimerHandle | None = getattr(self, "_generation_tick", None)
        if tick is not None:
            tick.cancel()
            self._generation_tick = None

    @staticmethod
    def _history_file(home: Path, workspace: Path) -> Path:
        workspace_hash = md5(str(workspace).encode("utf-8"), usedforsecurity=False).hexdigest()
        return home / "history" / f"{workspace_hash}.history"

    async def admit_message(
        self,
        session_id: str,
        message: Envelope,
        turn: TurnSnapshot,
    ) -> AdmitDecision | None:
        await self._echo_input(message.content, steering=turn.is_running)
        if not turn.is_running:
            return None
        return AdmitDecision("steer", reason="cli session is already generating")
