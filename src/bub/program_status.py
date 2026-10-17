"""Map application events to a single OSC 7501 root record."""

from contextlib import suppress
from typing import TextIO

from bub.application import CommandChanged, PromptChanged, PromptKind, WorkChanged
from bub.core.events import Event

_RESULTS = {None: "working", "succeeded": "done", "failed": "error", "cancelled": "idle"}


class ProgramStatus:
    def __init__(self, output: TextIO | None) -> None:
        self.output = output
        self.listener = False

    def __call__(self, event: Event) -> None:
        kind: PromptKind | None = None
        match event:
            case CommandChanged(outcome):
                state = "idle" if outcome == "succeeded" and self.listener else _RESULTS[outcome]
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
            self.output.write(f"\x1b]7501;{fields}\x1b\\")
            self.output.flush()
