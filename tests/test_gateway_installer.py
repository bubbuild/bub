from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bub.gateway_installer import (
    GatewayServiceError,
    install_gateway,
    is_gateway_service_supported,
    uninstall_gateway,
)


@pytest.mark.parametrize(
    ("platform", "expected"), [("linux", True), ("linux2", True), ("win32", True), ("darwin", False)]
)
def test_is_gateway_service_supported(platform: str, expected: bool) -> None:
    assert is_gateway_service_supported(platform) is expected


def test_install_gateway_writes_and_enables_systemd_user_unit(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    config_home = tmp_path / "config"
    commands: list[list[str]] = []
    monkeypatch.setattr("bub.gateway_installer.shutil.which", lambda command: "/usr/bin/systemctl")
    monkeypatch.setattr(
        "bub.gateway_installer.subprocess.run",
        lambda command, **kwargs: commands.append(command),
    )

    result = install_gateway(
        workspace,
        ["telegram", "!cron"],
        platform="linux",
        executable=Path("/opt/bub venv/bin/python"),
        environ={"XDG_CONFIG_HOME": os.fspath(config_home)},
        home=tmp_path,
    )

    unit_path = config_home / "systemd" / "user" / "bub-gateway.service"
    unit = unit_path.read_text(encoding="utf-8")

    assert result.backend == "systemd"
    assert result.destination == os.fspath(unit_path)
    assert f'WorkingDirectory="{workspace}"' in unit
    assert 'ExecStart="/opt/bub venv/bin/python" "-m" "bub"' in unit
    assert '"--enable-channel" "telegram" "--enable-channel" "!cron"' in unit
    assert "WantedBy=default.target" in unit
    assert commands == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "enable", "bub-gateway.service"],
        ["/usr/bin/systemctl", "--user", "restart", "bub-gateway.service"],
    ]


def test_install_gateway_registers_current_user_windows_task(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "project's workspace"
    workspace.mkdir()
    commands: list[tuple[list[str], dict]] = []
    monkeypatch.setattr("bub.gateway_installer.shutil.which", lambda command: r"C:\Windows\powershell.exe")
    monkeypatch.setattr(
        "bub.gateway_installer.subprocess.run",
        lambda command, **kwargs: commands.append((command, kwargs)),
    )

    result = install_gateway(
        workspace,
        ["telegram"],
        platform="win32",
        executable=Path(r"C:\Users\Frost\bub venv\python.exe"),
    )

    assert result.backend == "Windows Task Scheduler"
    assert result.destination == "Bub Gateway"
    assert len(commands) == 1
    command, kwargs = commands[0]
    script = command[-1]
    assert command[:4] == [r"C:\Windows\powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]
    assert "New-ScheduledTaskTrigger -AtLogOn -User $userId" in script
    assert "-LogonType Interactive -RunLevel Limited" in script
    assert "-RestartCount 999" in script
    assert "Stop-ScheduledTask -TaskName 'Bub Gateway'" in script
    assert "Register-ScheduledTask -TaskName 'Bub Gateway'" in script
    assert "Start-ScheduledTask -TaskName 'Bub Gateway'" in script
    assert "project''s workspace" in script
    assert kwargs == {"check": True, "capture_output": True, "text": True}


def test_install_gateway_rejects_unsupported_platform(tmp_path: Path) -> None:
    with pytest.raises(GatewayServiceError, match="supported only on Linux and Windows"):
        install_gateway(tmp_path, [], platform="darwin")


def test_install_gateway_reports_activation_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("bub.gateway_installer.shutil.which", lambda command: "/usr/bin/systemctl")

    def fail(command: list[str], **kwargs) -> None:
        raise subprocess.CalledProcessError(1, command, stderr="user bus unavailable")

    monkeypatch.setattr("bub.gateway_installer.subprocess.run", fail)

    with pytest.raises(GatewayServiceError, match="user bus unavailable"):
        install_gateway(tmp_path, [], platform="linux", environ={}, home=tmp_path)


def test_uninstall_gateway_disables_and_removes_systemd_user_unit(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / ".config" / "systemd" / "user" / "bub-gateway.service"
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("[Unit]\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr("bub.gateway_installer.shutil.which", lambda command: "/usr/bin/systemctl")
    monkeypatch.setattr(
        "bub.gateway_installer.subprocess.run",
        lambda command, **kwargs: commands.append(command),
    )

    result = uninstall_gateway(platform="linux", environ={}, home=tmp_path)

    assert result.backend == "systemd"
    assert result.destination == os.fspath(unit_path)
    assert not unit_path.exists()
    assert commands == [
        ["/usr/bin/systemctl", "--user", "disable", "--now", "bub-gateway.service"],
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
    ]


def test_uninstall_gateway_is_idempotent_when_systemd_unit_is_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bub.gateway_installer.shutil.which",
        lambda command: pytest.fail("systemctl should not be required when the unit is absent"),
    )

    result = uninstall_gateway(platform="linux", environ={}, home=tmp_path)

    assert result.backend == "systemd"
    assert not Path(result.destination).exists()


def test_uninstall_gateway_stops_and_unregisters_windows_task(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr("bub.gateway_installer.shutil.which", lambda command: r"C:\Windows\powershell.exe")
    monkeypatch.setattr(
        "bub.gateway_installer.subprocess.run",
        lambda command, **kwargs: commands.append(command),
    )

    result = uninstall_gateway(platform="win32")

    assert result.backend == "Windows Task Scheduler"
    assert len(commands) == 1
    script = commands[0][-1]
    assert "Get-ScheduledTask -TaskName 'Bub Gateway'" in script
    assert "Stop-ScheduledTask -TaskName 'Bub Gateway'" in script
    assert "Unregister-ScheduledTask -TaskName 'Bub Gateway' -Confirm:$false" in script
