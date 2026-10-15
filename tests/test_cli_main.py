import subprocess
import sys
from importlib.metadata import PackageNotFoundError

import pytest

import bub.__main__ as cli

REQUIREMENTS = [
    "republic>=0.6.0a1",
    "typer>=0.24.1; extra == 'app'",
    "httpx[socks]>=0.28.1; extra == 'app'",
    "opentelemetry-api>=1.39.0; extra == 'trace'",
]


def _installed(*names: str):
    def distribution(name: str) -> object:
        if name not in names:
            raise PackageNotFoundError(name)
        return object()

    return distribution


def test_missing_app_requirements_lists_only_uninstalled_app_extra_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "requires", lambda name: REQUIREMENTS)
    monkeypatch.setattr(cli, "distribution", _installed("republic", "typer"))

    assert cli.missing_app_requirements() == ["httpx"]


def test_bub_command_without_app_extra_exits_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "requires", lambda name: REQUIREMENTS)
    monkeypatch.setattr(cli, "distribution", _installed())

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    message = str(exc_info.value.code)
    assert "missing: typer, httpx" in message
    assert "pip install 'bub[app]'" in message


def test_bub_command_runs_the_cli_when_app_extra_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "missing_app_requirements", lambda: [])
    monkeypatch.setattr(cli, "create_cli_app", lambda: lambda: calls.append("app"))

    cli.main()

    assert calls == ["app"]


def test_uninstalled_source_tree_skips_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def requires(name: str) -> list[str]:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(cli, "requires", requires)

    assert cli.missing_app_requirements() == []


def test_importing_the_cli_module_does_not_load_the_app_extra() -> None:
    script = (
        "import sys, bub.__main__\n"
        "print(sorted({name.split('.')[0] for name in sys.modules} & {'typer', 'pluggy', 'pydantic_settings'}))\n"
        "print('bub.framework' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert result.stdout.splitlines() == ["[]", "False"]
