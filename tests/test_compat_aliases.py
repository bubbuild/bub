import importlib
import subprocess
import sys

import pytest

from bub.core import events
from bub.hooks import interception

MOVED_MODULES = {
    "bub.tape": "bub.core.tape",
    "bub.tools": "bub.core.tools",
    "bub.store": "bub.core.store",
    "bub.sidecars": "bub.core.sidecars",
    "bub.builtin.commands": "bub.core.commands",
    "bub.builtin.context": "bub.core.context",
}


@pytest.mark.parametrize(("old", "new"), MOVED_MODULES.items())
def test_moved_module_alias_is_the_new_module(old: str, new: str) -> None:
    assert importlib.import_module(old) is importlib.import_module(new)


def test_top_level_module_aliases_load_no_app_dependencies() -> None:
    script = (
        "import sys, bub.tape, bub.tools, bub.store, bub.sidecars\n"
        "loaded = {name.split('.')[0] for name in sys.modules}\n"
        "print(sorted(loaded & {'typer', 'pluggy', 'pydantic_settings', 'yaml', 'rapidfuzz'}))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == ["[]"]


@pytest.mark.parametrize("suffix", ["Request", "Result", "Decision"])
def test_interception_keeps_llm_call_payload_aliases(suffix: str) -> None:
    assert getattr(interception, f"LlmCall{suffix}") is getattr(events, f"LLMCall{suffix}")
    assert f"LlmCall{suffix}" in interception.__all__
