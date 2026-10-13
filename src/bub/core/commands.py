"""Command prompts: prefix rules and argument parsing."""

from __future__ import annotations

import shlex
from dataclasses import dataclass


def validate_command_prefix(prefix: str) -> str:
    if not prefix or any(char.isspace() for char in prefix):
        raise ValueError("command_prefix must be non-empty and contain no whitespace")
    return prefix


def strip_command_prefix(text: str, prefix: str) -> str | None:
    """Return the command without its prefix, or None for ordinary text.

    Strip surrounding whitespace and exactly one prefix. A bare prefix returns
    an empty string so execution can report an empty command.
    Callers validate the prefix when configuring it.
    """
    text = text.strip()
    if text.startswith(prefix):
        return text[len(prefix) :].strip()
    return None


@dataclass(frozen=True)
class CommandArgs:
    positional: list[str]
    kwargs: dict[str, str]


def parse_command(line: str) -> tuple[str, list[str]]:
    """Split a command line into its name and argument tokens, shell-style."""
    words = shlex.split(line.strip())
    if not words:
        return "", []
    return words[0], words[1:]


def parse_command_args(tokens: list[str]) -> CommandArgs:
    """Parse positional arguments followed by ``key=value`` arguments."""
    positional: list[str] = []
    kwargs: dict[str, str] = {}
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            kwargs[key] = value
        elif kwargs:
            raise ValueError(f"positional argument '{token}' cannot appear after keyword arguments")
        else:
            positional.append(token)
    return CommandArgs(positional=positional, kwargs=kwargs)
