"""Map application events to a single OSC 7501 root record."""

from contextlib import suppress
from typing import TextIO

from bub.application import CommandChanged, PromptChanged, PromptKind, WorkChanged
from bub.core.events import Event

_RESULTS = {None: "working", "succeeded": "done", "failed": "error", "cancelled": "idle"}
_CLEAR = "\x1b]7501;state=clear\x1b\\"


class ProgramStatus:
    def __init__(self, output: TextIO | None) -> None:
        self.output = output
        self.listener = False

    def __call__(self, event: Event) -> None:
        kind: PromptKind | None = None
        finished = False
        match event:
            case CommandChanged(outcome):
                state = "idle" if outcome == "succeeded" and self.listener else _RESULTS[outcome]
                finished = outcome is not None
            case WorkChanged(busy, completed):
                self.listener = True
                state = "working" if busy else _RESULTS[completed] if completed else "idle"
            case PromptChanged(kind):
                state = "blocked" if kind else "working"
            case _:
                return
        with suppress(OSError):
            if self.output is None or not self.output.isatty():
                return
            fields = f"state={state}:app=bub"
            if kind is not None:
                fields += f":kind={kind}"
            # The terminal outlives the command, so drop the record once the command ends.
            self.output.write(f"\x1b]7501;{fields}\x1b\\{_CLEAR if finished else ''}")
            self.output.flush()
