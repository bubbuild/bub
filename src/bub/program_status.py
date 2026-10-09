"""OSC 7501 reports for CLI commands, channel messages, and user prompts."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Literal, TextIO

import click

if TYPE_CHECKING:
    from bub.streaming import AsyncStreamEvents

type State = Literal["idle", "working", "blocked", "done", "error"]
type Kind = Literal["permission", "question", "auth"]

_REPORT_TEMPLATE = "\x1b]7501;%p1%s\x1b\\"


def _report_template(output: TextIO | None) -> str:
    """Read the spec's Pst capability once without querying terminal input."""
    with suppress(Exception):
        if output is not None and output.isatty():
            import curses

            curses.setupterm(fd=output.fileno())
            if capability := curses.tigetstr("Pst"):
                template = capability.decode("ascii")
                # Python's tparm only takes integers; Pst has one string parameter.
                if template in (_REPORT_TEMPLATE, _REPORT_TEMPLATE.removesuffix("\x1b\\") + "\x07"):
                    return template
    # A missing capability is unknown, so direct reports remain valid.
    return _REPORT_TEMPLATE


@dataclass
class _Status:
    output: TextIO | None
    id: str | None = None
    state: State = "working"
    result: State = "done"
    template: str = _REPORT_TEMPLATE

    def report(self, state: State, kind: Kind | None = None) -> None:
        self.state = state
        with suppress(Exception):
            if self.output is None or not self.output.isatty():
                return
            fields = f"state={state}:app=bub"
            if self.id is not None:
                fields += f":id={self.id}"
            if kind is not None:
                fields += f":kind={kind}"
            self.output.write(self.template.replace("%p1%s", fields))
            self.output.flush()


_current: ContextVar[_Status | None] = ContextVar("bub_program_status", default=None)
_ids = count(1)


@contextmanager
def program_status(output: TextIO | None, record_id: str | None = None) -> Iterator[None]:
    parent = _current.get()
    template = parent.template if parent is not None and record_id is not None else _report_template(output)
    status = _Status(output, record_id, template=template)
    token = _current.set(status)
    try:
        status.report("working")
        yield
    except (asyncio.CancelledError, KeyboardInterrupt, EOFError, click.Abort):
        status.result = "idle"
        raise
    except (SystemExit, click.exceptions.Exit) as exc:
        code = exc.code if isinstance(exc, SystemExit) else exc.exit_code
        if code not in (None, 0):
            status.result = "error"
        raise
    except BaseException:
        status.result = "error"
        raise
    finally:
        try:
            status.report(status.result)
        finally:
            _current.reset(token)


@contextmanager
def message_status() -> Iterator[None]:
    if (parent := _current.get()) is None:
        yield
        return
    with program_status(parent.output, f"turn-{next(_ids)}"):
        yield


def listener_ready() -> None:
    if (status := _current.get()) is not None:
        status.result = "idle"
        status.report("idle")


def message_failed() -> None:
    if (parent := _current.get()) is not None:
        _Status(parent.output, f"turn-{next(_ids)}", template=parent.template).report("error")


def model_failed() -> None:
    if (status := _current.get()) is not None:
        status.result = "error"


def model_finished(stream: AsyncStreamEvents) -> None:
    """Read the producer's final result without consuming or closing its stream."""
    if _current.get() is not None:
        with suppress(Exception):
            if stream.error is not None:
                model_failed()


@contextmanager
def waiting(kind: Kind) -> Iterator[None]:
    status = _current.get()
    if status is None or status.state == "blocked":
        yield
        return
    previous = status.state
    status.report("blocked", kind)
    try:
        yield
    finally:
        status.report(previous)
