import subprocess
import sys

import pytest

from bub.builtin.store import FuzzyFileTapeStore
from bub.core.store import FileTapeStore, TapeQuery
from bub.core.tape import TapeEntry

APP_MODULES = ("typer", "pluggy", "pydantic_settings", "dotenv", "yaml", "rapidfuzz", "telegram", "rich")


def test_importing_core_loads_no_framework_or_app_dependencies() -> None:
    script = (
        "import sys, bub.core\n"
        "loaded = {name.split('.')[0] for name in sys.modules}\n"
        f"print(sorted(loaded & set({APP_MODULES!r})))\n"
        "print(sorted(n for n in sys.modules if n.startswith('bub.') and not n.startswith('bub.core')))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == ["[]", "['bub._version']"]


@pytest.mark.parametrize("name", ["errors", "prompt", "tracing", "streaming", "store", "tape", "tools", "sidecars"])
def test_legacy_top_level_modules_alias_core_modules(name: str) -> None:
    __import__(f"bub.{name}")
    __import__(f"bub.core.{name}")

    assert sys.modules[f"bub.{name}"] is sys.modules[f"bub.core.{name}"]


def test_turn_state_is_shared_with_core() -> None:
    from bub.core.turn import TurnState as CoreTurnState
    from bub.turn import TurnState

    assert TurnState is CoreTurnState


def _search(store: FileTapeStore, text: str) -> list[str]:
    return [entry.payload["content"] for entry in store.fetch_all(TapeQuery("tape", store).query(text))]


def _write_messages(store: FileTapeStore) -> None:
    for content in ["deploy the gateway", "unrelated", "deploy the gateway", "deploy again"]:
        store.append("tape", TapeEntry.message({"role": "user", "content": content}))


def test_core_file_store_matches_text_in_order(tmp_path) -> None:
    store = FileTapeStore(tmp_path)
    _write_messages(store)

    assert _search(store, "deploy") == ["deploy the gateway", "deploy the gateway", "deploy again"]


def test_builtin_file_store_returns_newest_unique_matches_first(tmp_path) -> None:
    store = FuzzyFileTapeStore(tmp_path)
    _write_messages(store)

    assert _search(store, "deploy") == ["deploy again", "deploy the gateway"]
