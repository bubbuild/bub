from __future__ import annotations

from bub.core.tools import Tool


def builtin_tools() -> tuple[Tool, ...]:
    """The tools Bub's builtin plugin provides."""
    from bub.builtin import codemode, spill, tools

    return (*tools.TOOLS, *codemode.TOOLS, spill.spill_read)
