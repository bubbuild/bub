from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
INSTALL_SH = REPOSITORY_ROOT / "website" / "public" / "install.sh"
INSTALL_PS1 = REPOSITORY_ROOT / "website" / "public" / "install.ps1"


def find_command(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not available")
    return path


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def fake_uv_script() -> str:
    return """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$BUB_TEST_UV_LOG"
if [[ "$*" == "tool dir --bin" ]]; then
    printf '%s\\n' "$BUB_TEST_TOOL_BIN"
fi
"""


def installer_environment(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    return {
        **os.environ,
        "BUB_TEST_TOOL_BIN": str(tmp_path / "tool-bin"),
        "BUB_TEST_UV_LOG": str(tmp_path / "uv.log"),
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
    }


def test_install_sh_has_valid_bash_syntax() -> None:
    subprocess.run([find_command("bash"), "-n", str(INSTALL_SH)], check=True)


def test_install_sh_uses_existing_uv(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "uv", fake_uv_script())
    environment = installer_environment(tmp_path, bin_dir)

    result = subprocess.run(
        [find_command("bash"), str(INSTALL_SH)],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert (tmp_path / "uv.log").read_text().splitlines() == [
        "tool install bub@latest",
        "tool update-shell",
        "tool dir --bin",
    ]
    assert "Bub was installed successfully." in result.stdout


def test_install_sh_bootstraps_uv_into_home(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = tmp_path / "fake-uv"
    write_executable(fake_uv, fake_uv_script())

    uv_installer = tmp_path / "uv-installer.sh"
    uv_installer.write_text(
        """#!/bin/sh
mkdir -p "$UV_INSTALL_DIR"
cp "$BUB_TEST_FAKE_UV" "$UV_INSTALL_DIR/uv"
chmod +x "$UV_INSTALL_DIR/uv"
"""
    )
    write_executable(
        bin_dir / "curl",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$BUB_TEST_CURL_LOG"
cat "$BUB_TEST_UV_INSTALLER"
""",
    )

    environment = installer_environment(tmp_path, bin_dir)
    environment.update({
        "BUB_TEST_CURL_LOG": str(tmp_path / "curl.log"),
        "BUB_TEST_FAKE_UV": str(fake_uv),
        "BUB_TEST_UV_INSTALLER": str(uv_installer),
    })

    subprocess.run(
        [find_command("bash"), str(INSTALL_SH)],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert (tmp_path / "curl.log").read_text().strip() == "-LsSf https://astral.sh/uv/install.sh"
    assert (tmp_path / "home" / ".local" / "bin" / "uv").is_file()
    assert (tmp_path / "uv.log").read_text().splitlines() == [
        "tool install bub@latest",
        "tool update-shell",
        "tool dir --bin",
    ]


def test_install_ps1_has_expected_install_contract() -> None:
    content = INSTALL_PS1.read_text()

    assert '"https://astral.sh/uv/install.ps1"' in content
    assert '"bub@latest"' in content
    assert 'Invoke-Uv -Arguments @("tool", "install", $BubPackage)' in content
    assert "tool update-shell" in content
    assert 'Join-Path $HOME ".local\\bin"' in content


def test_install_ps1_parses_when_powershell_is_available() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    command = f"$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{INSTALL_PS1}', [ref]$null, [ref]$errors); if ($errors.Count) {{ exit 1 }}"
    subprocess.run([powershell, "-NoProfile", "-Command", command], check=True)
