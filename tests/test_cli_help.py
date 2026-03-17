from __future__ import annotations

import pytest

from bub.builtin.tools import show_help


@pytest.mark.asyncio
async def test_help_lists_correct_tool_names() -> None:
    help_text = await show_help.run()

    assert ",bash.output" in help_text
    assert ",bash.kill" in help_text

    assert ",bash_output" not in help_text
    assert ",kill_bash" not in help_text
