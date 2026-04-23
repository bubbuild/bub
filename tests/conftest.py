from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path

import pytest

import bub.configure as configure


@pytest.fixture(autouse=True)
def reset_loaded_config() -> Generator[None, None, None]:
    configure._global_config = None
    yield
    configure._global_config = None


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
        configure.load(config_file)
        return config_file

    return _load
