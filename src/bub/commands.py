"""Shared command prefix validation and recognition."""


def validate_command_prefix(prefix: str) -> str:
    if not prefix or any(char.isspace() for char in prefix):
        raise ValueError("command_prefix must be non-empty and contain no whitespace")
    return prefix


def parse_command(text: str, prefix: str = ",") -> str | None:
    """Return the command without its prefix, or None for ordinary text.

    A bare prefix returns an empty string so execution can report an empty command.
    Callers validate the prefix when configuring it.
    """
    text = text.strip()
    if text.startswith(prefix):
        return text[len(prefix) :].strip()
    return None
