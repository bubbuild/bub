from __future__ import annotations

import pytest
from loguru import logger

from bub.builtin.tools import show_help


@pytest.mark.asyncio
async def test_help_lists_correct_tool_names() -> None:
    help_text = await show_help.run()

    assert ",bash.output" in help_text
    assert ",bash.kill" in help_text

    assert ",bash_output" not in help_text
    assert ",kill_bash" not in help_text


def test_cli_instrumentation_disables_local_variable_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    from bub.__main__ import _instrument_bub

    sink_options: list[dict[str, object]] = []
    monkeypatch.setattr(logger, "remove", lambda: None)
    monkeypatch.setattr(logger, "add", lambda _sink, **options: sink_options.append(options))

    _instrument_bub()

    assert sink_options
    assert all(options.get("diagnose") is False for options in sink_options)
