from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import TextIO, cast

from prompt_toolkit.application import get_app_or_none
from prompt_toolkit.output.base import Output
from prompt_toolkit.output.vt100 import Vt100_Output

_BEGIN_SYNCHRONIZED_UPDATE = "\x1b[?2026h"
_END_SYNCHRONIZED_UPDATE = "\x1b[?2026l"


class SynchronizedVt100Output(Vt100_Output):
    """VT100 output that presents each rendered frame atomically."""

    _synchronized_depth = 0

    def flush(self) -> None:
        if not self._buffer:
            return
        if self._synchronized_depth == 0:
            self._buffer.insert(0, _BEGIN_SYNCHRONIZED_UPDATE)
            self._buffer.append(_END_SYNCHRONIZED_UPDATE)
        super().flush()

    @contextmanager
    def synchronized_update(self) -> Iterator[None]:
        if self._synchronized_depth == 0:
            self.write_raw(_BEGIN_SYNCHRONIZED_UPDATE)
            super().flush()
        self._synchronized_depth += 1
        try:
            yield
        finally:
            self._synchronized_depth -= 1
            if self._synchronized_depth == 0:
                self.write_raw(_END_SYNCHRONIZED_UPDATE)
                super().flush()


def create_synchronized_output(stdout: TextIO | None = None) -> Output | None:
    target = stdout if stdout is not None else sys.stdout
    if sys.platform == "win32" or not target.isatty():
        return None
    return cast(SynchronizedVt100Output, SynchronizedVt100Output.from_pty(target))


def _original_stream(stream: TextIO) -> TextIO:
    original = getattr(stream, "original_stdout", None)
    return cast(TextIO, original) if original is not None else stream


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
        # We are on the application loop; a direct redraw keeps this frame inside
        # the synchronized-output context instead of waiting for the redraw throttle.
        app._redraw()


@contextmanager
def synchronized_prompt_output() -> Iterator[None]:
    app = get_app_or_none()
    output = app.output if app is not None else None
    if isinstance(output, SynchronizedVt100Output):
        with output.synchronized_update():
            yield
        return
    yield
