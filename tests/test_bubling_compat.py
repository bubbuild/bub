import subprocess
import sys

import pytest

from bub.builtin.store import FuzzyFileTapeStore
from bubling.store import FileTapeStore, TapeQuery
from bubling.tape import TapeEntry

APP_MODULES = ("typer", "pluggy", "pydantic_settings", "dotenv", "yaml", "rapidfuzz", "telegram", "rich")


def test_importing_bubling_loads_no_bub_or_app_dependencies() -> None:
    script = (
        "import sys, bubling\n"
        "loaded = {name.split('.')[0] for name in sys.modules}\n"
        f"print(sorted(loaded & set({APP_MODULES!r} + ('bub',))))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == ["[]"]


@pytest.mark.parametrize("name", ["errors", "tracing", "streaming", "store", "tape", "tools", "sidecars"])
def test_legacy_top_level_modules_alias_bubling_modules(name: str) -> None:
    __import__(f"bub.{name}")
    __import__(f"bubling.{name}")

    assert sys.modules[f"bub.{name}"] is sys.modules[f"bubling.{name}"]


def test_turn_state_is_shared_with_bubling() -> None:
    from bub.turn import TurnState
    from bubling.turn import TurnState as BublingTurnState

    assert TurnState is BublingTurnState


def _search(store: FileTapeStore, text: str) -> list[str]:
    return [entry.payload["content"] for entry in store.fetch_all(TapeQuery("tape", store).query(text))]


def _write_messages(store: FileTapeStore) -> None:
    for content in ["deploy the gateway", "unrelated", "deploy the gateway", "deploy again"]:
        store.append("tape", TapeEntry.message({"role": "user", "content": content}))


def test_bubling_file_store_matches_text_in_order(tmp_path) -> None:
    store = FileTapeStore(tmp_path)
    _write_messages(store)

    assert _search(store, "deploy") == ["deploy the gateway", "deploy the gateway", "deploy again"]


def test_builtin_file_store_returns_newest_unique_matches_first(tmp_path) -> None:
    store = FuzzyFileTapeStore(tmp_path)
    _write_messages(store)

    assert _search(store, "deploy") == ["deploy again", "deploy the gateway"]
