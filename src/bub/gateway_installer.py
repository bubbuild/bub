"""Install the Bub gateway as a per-user background service."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SYSTEMD_UNIT_NAME = "bub-gateway.service"
WINDOWS_TASK_NAME = "Bub Gateway"


class GatewayServiceError(RuntimeError):
    """Raised when the gateway service cannot be installed or removed."""


@dataclass(frozen=True)
class GatewayServiceResult:
    backend: str
    destination: str


def is_gateway_service_supported(platform: str | None = None) -> bool:
    """Return whether the current platform has a supported per-user service backend."""
    platform = sys.platform if platform is None else platform
    return platform.startswith("linux") or platform == "win32"


def install_gateway(
    workspace: Path,
    enable_channels: Sequence[str],
    *,
    platform: str | None = None,
    executable: Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> GatewayServiceResult:
    """Install and start a gateway service for the current user."""
    platform = sys.platform if platform is None else platform
    executable = Path(sys.executable) if executable is None else executable
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    workspace = workspace.resolve()

    if platform.startswith("linux"):
        return _install_systemd(workspace, enable_channels, executable, environ, home)
    if platform == "win32":
        return _install_windows_task(workspace, enable_channels, executable)
    raise GatewayServiceError("Gateway installation is supported only on Linux and Windows.")


def uninstall_gateway(
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> GatewayServiceResult:
    """Stop and uninstall the gateway service for the current user."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home

    if platform.startswith("linux"):
        return _uninstall_systemd(environ, home)
    if platform == "win32":
        return _uninstall_windows_task()
    raise GatewayServiceError("Gateway uninstallation is supported only on Linux and Windows.")


def _gateway_arguments(workspace: Path, enable_channels: Sequence[str]) -> list[str]:
    arguments = ["-m", "bub", "--workspace", os.fspath(workspace), "gateway"]
    for channel in enable_channels:
        arguments.extend(["--enable-channel", channel])
    return arguments


def _systemd_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _install_systemd(
    workspace: Path,
    enable_channels: Sequence[str],
    executable: Path,
    environ: Mapping[str, str],
    home: Path,
) -> GatewayServiceResult:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise GatewayServiceError("systemctl was not found; a systemd user manager is required.")

    unit_path = _systemd_unit_path(environ, home)
    command = [os.fspath(executable), *_gateway_arguments(workspace, enable_channels)]
    exec_start = " ".join(_systemd_quote(argument) for argument in command)
    unit = f"""[Unit]
Description=Bub gateway
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={_systemd_quote(os.fspath(workspace))}
ExecStart={exec_start}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
"""

    try:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit, encoding="utf-8")
        _run([systemctl, "--user", "daemon-reload"])
        _run([systemctl, "--user", "enable", SYSTEMD_UNIT_NAME])
        _run([systemctl, "--user", "restart", SYSTEMD_UNIT_NAME])
    except OSError as exc:
        raise GatewayServiceError(f"Failed to write {unit_path}: {exc}") from exc

    return GatewayServiceResult(backend="systemd", destination=os.fspath(unit_path))


def _systemd_unit_path(environ: Mapping[str, str], home: Path) -> Path:
    config_home = Path(environ.get("XDG_CONFIG_HOME", home / ".config")).expanduser()
    return config_home / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _uninstall_systemd(environ: Mapping[str, str], home: Path) -> GatewayServiceResult:
    unit_path = _systemd_unit_path(environ, home)
    if not unit_path.exists():
        return GatewayServiceResult(backend="systemd", destination=os.fspath(unit_path))

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise GatewayServiceError("systemctl was not found; the systemd user unit was not removed.")

    _run([systemctl, "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
    try:
        unit_path.unlink()
    except OSError as exc:
        raise GatewayServiceError(f"Failed to remove {unit_path}: {exc}") from exc
    _run([systemctl, "--user", "daemon-reload"])
    return GatewayServiceResult(backend="systemd", destination=os.fspath(unit_path))


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _install_windows_task(
    workspace: Path,
    enable_channels: Sequence[str],
    executable: Path,
) -> GatewayServiceResult:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        raise GatewayServiceError("Windows PowerShell was not found; Task Scheduler installation is unavailable.")

    arguments = subprocess.list2cmdline(_gateway_arguments(workspace, enable_channels))
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        f"Stop-ScheduledTask -TaskName {_powershell_quote(WINDOWS_TASK_NAME)} -ErrorAction SilentlyContinue",
        (
            "$action = New-ScheduledTaskAction "
            f"-Execute {_powershell_quote(os.fspath(executable))} "
            f"-Argument {_powershell_quote(arguments)} "
            f"-WorkingDirectory {_powershell_quote(os.fspath(workspace))}"
        ),
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId",
        ("$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited"),
        (
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
            "-MultipleInstances IgnoreNew -RestartCount 999 "
            "-RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable"
        ),
        (
            f"Register-ScheduledTask -TaskName {_powershell_quote(WINDOWS_TASK_NAME)} "
            "-Action $action -Trigger $trigger -Principal $principal -Settings $settings "
            "-Description 'Run the Bub gateway for the current user.' -Force | Out-Null"
        ),
        f"Start-ScheduledTask -TaskName {_powershell_quote(WINDOWS_TASK_NAME)}",
    ])
    _run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script])
    return GatewayServiceResult(backend="Windows Task Scheduler", destination=WINDOWS_TASK_NAME)


def _uninstall_windows_task() -> GatewayServiceResult:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        raise GatewayServiceError("Windows PowerShell was not found; Task Scheduler uninstallation is unavailable.")

    task_name = _powershell_quote(WINDOWS_TASK_NAME)
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$task = Get-ScheduledTask -TaskName {task_name} -ErrorAction SilentlyContinue",
        "if ($null -ne $task) {",
        f"    Stop-ScheduledTask -TaskName {task_name} -ErrorAction SilentlyContinue",
        f"    Unregister-ScheduledTask -TaskName {task_name} -Confirm:$false",
        "}",
    ])
    _run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script])
    return GatewayServiceResult(backend="Windows Task Scheduler", destination=WINDOWS_TASK_NAME)


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise GatewayServiceError(f"Required command was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise GatewayServiceError(f"Command failed ({command[0]}): {detail}") from exc
