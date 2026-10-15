from __future__ import annotations

import os
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import httpx2
import pytest

import bub.configure as configure
from tests.model_fakes import ProviderService


@pytest.fixture(autouse=True)
def isolate_bub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Importing bub.framework loads the developer's .env into os.environ; tests set what they need.
    for name in list(os.environ):
        if name.startswith("BUB_"):
            monkeypatch.delenv(name)


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


@pytest.fixture
def provider_service() -> ProviderService:
    return ProviderService()


@pytest.fixture
def codex_executable(tmp_path: Path) -> Path:
    import sys

    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
from pathlib import Path
from typing import Any
args = sys.argv[1:]
if not args or args[0] != "login" or 'cli_auth_credentials_store="file"' not in args:
    sys.exit(2)
home = Path(os.environ["CODEX_HOME"])
home.mkdir(parents=True, exist_ok=True)
account = "device-account" if "--device-auth" in args else "browser-account"
(home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "test-token", "account_id": account}}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


@pytest.fixture
def provider_transport(provider_service: ProviderService, monkeypatch: pytest.MonkeyPatch) -> ProviderService:
    constructor = httpx2.AsyncClient

    def client(**kwargs: Any) -> httpx2.AsyncClient:
        kwargs.setdefault("transport", httpx2.MockTransport(provider_service._respond))
        return constructor(**kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    return provider_service
