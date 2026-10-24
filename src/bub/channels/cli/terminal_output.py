from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import TextIO, cast

from prompt_toolkit.application import get_app_or_none, run_in_terminal
from prompt_toolkit.output.base import Output
from prompt_toolkit.output.vt100 import Vt100_Output

_BEGIN_SYNCHRONIZED_UPDATE = "\x1b[?2026h"
_END_SYNCHRONIZED_UPDATE = "\x1b[?2026l"


class _SynchronizedTextIO:
    """Wrap complete terminal writes without reaching into prompt_toolkit buffers."""

    def __init__(self, target: TextIO) -> None:
        self._target = target
        self._depth = 0

    @property
    def encoding(self) -> str | None:
        return self._target.encoding

    def fileno(self) -> int:
        return self._target.fileno()

    def isatty(self) -> bool:
        return self._target.isatty()

    def write(self, data: str) -> int:
        if self._depth:
            return self._target.write(data)
        return self._target.write(f"{_BEGIN_SYNCHRONIZED_UPDATE}{data}{_END_SYNCHRONIZED_UPDATE}")

    def flush(self) -> None:
        self._target.flush()

    @contextmanager
    def synchronized_update(self) -> Iterator[None]:
        if self._depth == 0:
            self._target.write(_BEGIN_SYNCHRONIZED_UPDATE)
            self._target.flush()
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._target.write(_END_SYNCHRONIZED_UPDATE)
                self._target.flush()


class SynchronizedVt100Output(Vt100_Output):
    """VT100 output that presents each rendered frame atomically."""

    @contextmanager
    def synchronized_update(self) -> Iterator[None]:
        self.flush()
        output = cast(_SynchronizedTextIO, self.stdout)
        with output.synchronized_update():
            yield
            self.flush()


def _original_stream(stream: TextIO) -> TextIO:
    original = getattr(stream, "original_stdout", None)
    return cast(TextIO, original) if original is not None else stream


def create_synchronized_output(stdout: TextIO | None = None) -> Output | None:
    # The prompt may be built inside ``patch_stdout``; rendering into its proxy would
    # make every frame re-enter ``run_in_terminal`` and redraw the prompt forever.
    target = _original_stream(stdout if stdout is not None else sys.stdout)
    if sys.platform == "win32" or not target.isatty():
        return None
    synchronized_stdout = cast(TextIO, _SynchronizedTextIO(target))
    return cast(SynchronizedVt100Output, SynchronizedVt100Output.from_pty(synchronized_stdout))


@contextmanager
def direct_terminal_stdio() -> Iterator[None]:
    """Bypass prompt_toolkit's deferred stdout proxy for an active terminal callback."""
    with redirect_stdout(_original_stream(sys.stdout)), redirect_stderr(_original_stream(sys.stderr)):
        yield


async def restore_synchronized_prompt() -> None:
    """Finish the CPR-dependent toolbar redraw before presenting a synchronized frame."""
    app = get_app_or_none()
    if app is None or not app.is_running or not isinstance(app.output, SynchronizedVt100Output):
        return
    if app.renderer.waiting_for_cpr:
        await app.renderer.wait_for_cpr_responses()
    if app.is_running and not app.is_done and app.renderer.height_is_known:
        prompt_finished = app.future
        if prompt_finished is None:
            return
        rendered = asyncio.get_running_loop().create_future()

        def after_render(_) -> None:
            if not rendered.done():
                rendered.set_result(None)

        app.after_render.add_handler(after_render)
        try:
            app.invalidate()
            await asyncio.wait((rendered, prompt_finished), return_when=asyncio.FIRST_COMPLETED)
        finally:
            app.after_render.remove_handler(after_render)


@contextmanager
def synchronized_prompt_output() -> Iterator[None]:
    app = get_app_or_none()
    output = app.output if app is not None else None
    if isinstance(output, SynchronizedVt100Output):
        with output.synchronized_update():
            yield
        return
    yield


class TerminalPresenter:
    """Serialize every write that temporarily interrupts the active prompt."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def write(self, function: Callable[[], None]) -> None:
        async with self._lock:

            def write_directly() -> None:
                with direct_terminal_stdio():
                    function()

            with synchronized_prompt_output():
                await run_in_terminal(write_directly, render_cli_done=False)
                await restore_synchronized_prompt()
