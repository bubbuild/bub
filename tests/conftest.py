from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path

import pytest

import bub.configure as configure


@pytest.fixture(autouse=True)
def reset_loaded_config() -> Generator[None, None, None]:
    configure._global_config.clear()
    configure._config_data.clear()
    yield
    configure._global_config.clear()
    configure._config_data.clear()


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    def _write(content: str = "") -> Path:
        config_file = tmp_path / "config.yml"
        config_file.write_text(content, encoding="utf-8")
        return config_file

    return _write


@pytest.fixture
def load_config(write_config: Callable[[str], Path], monkeypatch: pytest.MonkeyPatch) -> Callable[[str], Path]:
    def _load(content: str = "") -> Path:
        config_file = write_config(content)
        monkeypatch.chdir(config_file.parent)
        configure._global_config.clear()
        configure.load(config_file)
        return config_file

    return _load


@pytest.fixture(autouse=True)
def isolate_codex_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Even model-selection existence checks must never consult real user files.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


@pytest.fixture(autouse=True)
def block_unmocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any missed SDK transport injection fails locally, before network I/O."""
    import httpx

    async def deny_async(*args, **kwargs):
        raise AssertionError("Tests require an explicit HTTP MockTransport")

    def deny_sync(*args, **kwargs):
        raise AssertionError("Tests require an explicit HTTP MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)
