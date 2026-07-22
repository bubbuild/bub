from __future__ import annotations

import re
from io import StringIO

from rich.console import Console, RenderableType

# prompt_toolkit's ANSI parser does not understand OSC 8 hyperlinks. Marking
# each OSC sequence as zero-width preserves the link without affecting layout.
_OSC8_RE = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x1b\\|\x07)")


def render_to_ansi(renderable: RenderableType, *, width: int | None = None) -> str:
    """Render a Rich object as ANSI text consumable by prompt_toolkit."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        highlight=False,
        width=width,
    )
    console.print(renderable, end="")
    return _OSC8_RE.sub(lambda match: f"\x01{match.group(0)}\x02", output.getvalue())
