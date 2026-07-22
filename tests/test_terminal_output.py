from __future__ import annotations

from io import StringIO

from prompt_toolkit.data_structures import Size

from bub.channels.cli.terminal_output import SynchronizedVt100Output, create_synchronized_output

_BEGIN_SYNCHRONIZED_UPDATE = "\x1b[?2026h"
_END_SYNCHRONIZED_UPDATE = "\x1b[?2026l"


def _output(stream: StringIO) -> SynchronizedVt100Output:
    return SynchronizedVt100Output(
        stream,
        lambda: Size(rows=24, columns=80),
        term="xterm-256color",
        enable_cpr=False,
    )


def test_synchronized_output_flushes_one_atomic_frame() -> None:
    stream = StringIO()
    output = _output(stream)

    output.write_raw("\x1b[?25lpaint\x1b[?25h")
    output.flush()

    assert stream.getvalue() == (
        f"{_BEGIN_SYNCHRONIZED_UPDATE}\x1b[?25lpaint\x1b[?25h{_END_SYNCHRONIZED_UPDATE}"
    )


def test_synchronized_update_groups_multiple_flushes() -> None:
    stream = StringIO()
    output = _output(stream)

    with output.synchronized_update():
        output.write_raw("erase prompt")
        output.flush()
        with output.synchronized_update():
            output.write_raw("print committed block")
            output.flush()
        output.write_raw("restore prompt")
        output.flush()

    assert stream.getvalue() == (
        f"{_BEGIN_SYNCHRONIZED_UPDATE}"
        "erase promptprint committed blockrestore prompt"
        f"{_END_SYNCHRONIZED_UPDATE}"
    )


def test_create_synchronized_output_keeps_non_tty_default() -> None:
    assert create_synchronized_output(StringIO()) is None


def test_create_synchronized_output_keeps_windows_default(monkeypatch) -> None:
    class TtyStringIO(StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("bub.channels.cli.terminal_output.sys.platform", "win32")

    assert create_synchronized_output(TtyStringIO()) is None
