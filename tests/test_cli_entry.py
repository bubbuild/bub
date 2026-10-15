from importlib.metadata import PackageNotFoundError

import pytest

from bub import _cli

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
    monkeypatch.setattr(_cli, "requires", lambda name: REQUIREMENTS)
    monkeypatch.setattr(_cli, "distribution", _installed("republic", "typer"))

    assert _cli.missing_app_requirements() == ["httpx"]


def test_bub_command_without_app_extra_exits_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "requires", lambda name: REQUIREMENTS)
    monkeypatch.setattr(_cli, "distribution", _installed())

    with pytest.raises(SystemExit) as exc_info:
        _cli.main()

    message = str(exc_info.value.code)
    assert "missing: typer, httpx" in message
    assert "pip install 'bub[app]'" in message


def test_bub_command_runs_the_cli_when_app_extra_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    import bub.__main__

    calls: list[str] = []
    monkeypatch.setattr(_cli, "missing_app_requirements", lambda: [])
    monkeypatch.setattr(bub.__main__, "app", lambda: calls.append("app"))

    _cli.main()

    assert calls == ["app"]


def test_uninstalled_source_tree_skips_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    def requires(name: str) -> list[str]:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(_cli, "requires", requires)

    assert _cli.missing_app_requirements() == []
