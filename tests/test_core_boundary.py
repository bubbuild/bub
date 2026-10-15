import subprocess
import sys

from bub.builtin.store import FuzzyFileTapeStore
from bub.core.store import FileTapeStore, TapeQuery
from bub.core.tape import TapeEntry

APP_MODULES = ("typer", "pluggy", "pydantic_settings", "dotenv", "yaml", "rapidfuzz", "telegram", "rich")


def test_importing_core_loads_no_app_dependencies() -> None:
    script = (
        "import sys, bub.core\n"
        "loaded = {name.split('.')[0] for name in sys.modules}\n"
        f"print(sorted(loaded & set({APP_MODULES!r})))\n"
        "print('bub.framework' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == ["[]", "False"]


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
