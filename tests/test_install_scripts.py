from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
INSTALL_SH = REPOSITORY_ROOT / "website" / "public" / "install.sh"
INSTALL_PS1 = REPOSITORY_ROOT / "website" / "public" / "install.ps1"
PRESETS_JSON = REPOSITORY_ROOT / "website" / "public" / "presets.json"


def find_command(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} is not available")
    return path


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def load_preset_catalog() -> dict[str, object]:
    return json.loads(PRESETS_JSON.read_text())


def preset_dependencies(name: str) -> list[str]:
    presets = load_preset_catalog()["presets"]
    return next(preset["dependencies"] for preset in presets if preset["name"] == name)


def embedded_bash_resolver() -> str:
    content = INSTALL_SH.read_text()
    return content.partition("<<'PY'\n")[2].partition("\nPY\n")[0]


def embedded_powershell_resolver() -> str:
    content = INSTALL_PS1.read_text()
    return content.partition("$EmbeddedPython = @'\n")[2].partition("\n'@\n")[0]


def fake_uv_script() -> str:
    return """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$BUB_TEST_UV_LOG"
if [[ "$1" == "run" && "$2" == "--no-project" && "$3" == "python" && "$4" == "-" ]]; then
    shift 4
    exec "$BUB_TEST_PYTHON" - "$@"
fi
if [[ "$1" == "venv" ]]; then
    venv_path=""
    for argument in "$@"; do
        venv_path=$argument
    done
    mkdir -p "$venv_path/bin"
    touch "$venv_path/bin/python"
    chmod +x "$venv_path/bin/python"
fi
if [[ "$1" == "pip" && "$2" == "install" ]]; then
    shift 2
    python_path=""
    while (($#)); do
        if [[ "$1" == "--python" ]]; then
            python_path=$2
            break
        fi
        shift
    done
    [[ -n "$python_path" ]]
    cp "$BUB_TEST_FAKE_BUB" "$(dirname "$python_path")/bub"
    chmod +x "$(dirname "$python_path")/bub"
fi
"""


def fake_curl_script() -> str:
    return """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$BUB_TEST_CURL_LOG"
if [[ "$*" == *"astral.sh/uv/install.sh"* ]]; then
    cat "$BUB_TEST_UV_INSTALLER"
    exit 0
fi
destination=""
while (($#)); do
    if [[ "$1" == "-o" ]]; then
        destination=$2
        break
    fi
    shift
done
[[ -n "$destination" ]]
cp "$BUB_TEST_PRESETS" "$destination"
"""


def installer_environment(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    home_dir = tmp_path / "home"
    home_dir.mkdir(exist_ok=True)
    fake_bub = tmp_path / "fake-bub"
    write_executable(
        fake_bub,
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$BUB_TEST_BUB_LOG"
""",
    )
    write_executable(bin_dir / "curl", fake_curl_script())
    return {
        **os.environ,
        "BUB_INSTALLER_PRESETS_URL": "https://example.test/presets.json",
        "BUB_TEST_BUB_LOG": str(tmp_path / "bub.log"),
        "BUB_TEST_CURL_LOG": str(tmp_path / "curl.log"),
        "BUB_TEST_FAKE_BUB": str(fake_bub),
        "BUB_TEST_PRESETS": str(PRESETS_JSON),
        "BUB_TEST_PYTHON": sys.executable,
        "BUB_TEST_UV_LOG": str(tmp_path / "uv.log"),
        "HOME": str(home_dir),
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "TMPDIR": str(tmp_path),
    }


def run_installer(
    tmp_path: Path,
    environment: dict[str, str],
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [find_command("bash"), str(INSTALL_SH), *arguments],
        check=check,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )


def test_install_sh_has_valid_bash_syntax() -> None:
    subprocess.run([find_command("bash"), "-n", str(INSTALL_SH)], check=True)


def test_preset_catalog_has_valid_public_contract() -> None:
    catalog = load_preset_catalog()

    assert catalog["schema_version"] == 1
    assert sum(preset["default"] for preset in catalog["presets"]) == 1
    assert {"minimal", "recommended"} <= {preset["name"] for preset in catalog["presets"]}
    assert all(isinstance(preset["dependencies"], list) for preset in catalog["presets"])


def test_install_sh_uses_existing_uv_with_minimal_preset(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "uv", fake_uv_script())
    environment = installer_environment(tmp_path, bin_dir)
    old_executable = tmp_path / "home" / ".local" / "bin" / "bub"
    old_executable.parent.mkdir(parents=True)
    write_executable(old_executable, "#!/usr/bin/env bash\nexit 0\n")

    result = run_installer(tmp_path, environment, "--preset", "minimal")

    uv_calls = (tmp_path / "uv.log").read_text().splitlines()
    assert uv_calls[0].startswith("run --no-project python - ")
    assert " noninteractive minimal " in uv_calls[0]
    venv_path = tmp_path / "home" / ".bub" / ".venv"
    assert uv_calls[1:] == [
        f"venv --python 3.12 --allow-existing {venv_path}",
        f"pip install --python {venv_path / 'bin' / 'python'} --upgrade bub",
    ]
    executable_link = tmp_path / "home" / ".local" / "bin" / "bub"
    assert executable_link.is_symlink()
    assert executable_link.resolve() == venv_path / "bin" / "bub"
    assert not (tmp_path / "bub.log").exists()
    assert "Bub was installed successfully." in result.stdout
    assert "Preset: minimal" in result.stdout
    assert "\033[" not in result.stdout
    assert not list(tmp_path.glob("bub-presets.*"))
    assert not list(tmp_path.glob("bub-resolution.*"))


def test_install_sh_installs_preset_and_extra_dependencies(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "uv", fake_uv_script())
    environment = installer_environment(tmp_path, bin_dir)

    run_installer(
        tmp_path,
        environment,
        "--preset=recommended",
        "--dependency",
        "extra-plugin",
        "--plugin=bub-mcp@main",
    )

    expected_dependencies = [*preset_dependencies("recommended"), "extra-plugin"]
    assert (tmp_path / "bub.log").read_text().splitlines() == [f"install -- {' '.join(expected_dependencies)}"]


def test_install_sh_rejects_unknown_preset_before_installing_bub(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "uv", fake_uv_script())
    environment = installer_environment(tmp_path, bin_dir)

    result = run_installer(tmp_path, environment, "--preset", "missing", check=False)

    assert result.returncode != 0
    available_presets = ", ".join(preset["name"] for preset in load_preset_catalog()["presets"])
    assert f"available presets: {available_presets}" in result.stderr
    assert (tmp_path / "uv.log").read_text().count("pip install") == 0
    assert not (tmp_path / "bub.log").exists()


def test_install_sh_rejects_unsafe_dependency(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable(bin_dir / "uv", fake_uv_script())
    environment = installer_environment(tmp_path, bin_dir)

    result = run_installer(tmp_path, environment, "--preset", "minimal", "--dependency=-unsafe", check=False)

    assert result.returncode != 0
    assert "not a safe package specification" in result.stderr
    assert (tmp_path / "uv.log").read_text().count("pip install") == 0


def test_install_sh_requires_preset_without_terminal(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    environment = installer_environment(tmp_path, bin_dir)

    result = run_installer(tmp_path, environment, check=False)

    assert result.returncode != 0
    assert "no interactive terminal is available" in result.stderr


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

    environment = installer_environment(tmp_path, bin_dir)
    environment.update({
        "BUB_TEST_FAKE_UV": str(fake_uv),
        "BUB_TEST_UV_INSTALLER": str(uv_installer),
    })

    run_installer(tmp_path, environment, "--preset", "minimal")

    assert (tmp_path / "home" / ".local" / "bin" / "uv").is_file()
    assert (tmp_path / "curl.log").read_text().splitlines() == [
        "-LsSf https://astral.sh/uv/install.sh",
        "-LsSf https://example.test/presets.json -o "
        + next(
            part.removeprefix("run --no-project python - ").rsplit(" noninteractive minimal ", maxsplit=1)[0]
            for part in (tmp_path / "uv.log").read_text().splitlines()
            if part.startswith("run --no-project python - ")
        ),
    ]


def test_install_scripts_expose_interactive_color_and_onboarding_contract() -> None:
    bash_content = INSTALL_SH.read_text()
    powershell_content = INSTALL_PS1.read_text()

    for content in (bash_content, powershell_content):
        assert "NO_COLOR" in content
        assert "--preset" in content
        assert "--dependency" in content
        assert ".bub" in content
        assert ".venv" in content
        assert "pip" in content
        assert "presets.json" in content
        assert "onboard" in content
        assert "install" in content
        assert "inquirer-textual==0.6.1" in content
        assert "prompts.select" in content
        assert "tool install" not in content
    assert "$'\\033[" in bash_content
    assert 'local venv_dir="$bub_root/.venv"' in bash_content
    assert 'local executable_dir="$HOME/.local/bin"' in bash_content
    assert 'ln -s "$source" "$destination"' in bash_content
    assert "ForegroundColor" in powershell_content
    assert '$VenvPath = Join-Path $BubRoot ".venv"' in powershell_content
    assert '$ExecutableDirectory = Join-Path $HOME ".local\\bin"' in powershell_content
    assert "New-Item -ItemType HardLink" in powershell_content
    assert '"bub-mcp@main"' in PRESETS_JSON.read_text()


def test_embedded_resolver_attaches_textual_standard_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    source = embedded_bash_resolver()
    assert source == embedded_powershell_resolver()

    namespace: dict[str, object] = {"__name__": "installer_resolver"}
    exec(compile(source, "installer_resolver.py", "exec"), namespace)  # noqa: S102

    input_stream = StringIO()
    output_stream = StringIO()
    original_streams = (sys.stdin, sys.stdout, sys.stderr, sys.__stdin__, sys.__stdout__, sys.__stderr__)

    def open_terminal(path: str, mode: str = "r", **_: object) -> StringIO:
        assert path == "/dev/tty"
        return output_stream if "w" in mode else input_stream

    monkeypatch.setattr("builtins.open", open_terminal)
    try:
        namespace["attach_terminal"]()
        assert sys.stdin is sys.__stdin__ is input_stream
        assert sys.stdout is sys.__stdout__ is output_stream
        assert sys.stderr is sys.__stderr__ is output_stream
    finally:
        sys.stdin, sys.stdout, sys.stderr, sys.__stdin__, sys.__stdout__, sys.__stderr__ = original_streams


def test_embedded_resolver_interactive_picker_uses_inquirer_textual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = embedded_bash_resolver()
    assert source == embedded_powershell_resolver()

    namespace: dict[str, object] = {"__name__": "installer_resolver"}
    # Execute the checked-in embedded resolver so its interactive contract can be tested directly.
    exec(compile(source, "installer_resolver.py", "exec"), namespace)  # noqa: S102

    calls: list[tuple[str, object]] = []

    class FakeChoice:
        def __init__(self, name: str, data: object = None) -> None:
            self.name = name
            self.data = data

    class FakePromptSettings:
        def __init__(self, **options: object) -> None:
            self.options = options

    prompts = ModuleType("inquirer_textual.prompts")

    def select(message: str, choices: list[FakeChoice], **options: object) -> SimpleNamespace:
        calls.append(("select", (message, choices, options)))
        return SimpleNamespace(value=options["default"])

    def text(message: str, **options: object) -> SimpleNamespace:
        calls.append(("text", (message, options)))
        return SimpleNamespace(value="extra-plugin")

    prompts.select = select
    prompts.text = text
    package = ModuleType("inquirer_textual")
    package.prompts = prompts
    common_package = ModuleType("inquirer_textual.common")
    choice_module = ModuleType("inquirer_textual.common.Choice")
    choice_module.Choice = FakeChoice
    settings_module = ModuleType("inquirer_textual.common.PromptSettings")
    settings_module.PromptSettings = FakePromptSettings
    monkeypatch.setitem(sys.modules, "inquirer_textual", package)
    monkeypatch.setitem(sys.modules, "inquirer_textual.prompts", prompts)
    monkeypatch.setitem(sys.modules, "inquirer_textual.common", common_package)
    monkeypatch.setitem(sys.modules, "inquirer_textual.common.Choice", choice_module)
    monkeypatch.setitem(sys.modules, "inquirer_textual.common.PromptSettings", settings_module)
    namespace["attach_terminal"] = lambda: None
    resolution_path = tmp_path / "resolution.txt"
    monkeypatch.setattr(
        sys,
        "argv",
        ["installer_resolver.py", str(PRESETS_JSON), "interactive", "", str(resolution_path)],
    )

    namespace["main"]()

    expected_dependencies = [*preset_dependencies("recommended"), "extra-plugin"]
    assert resolution_path.read_text().splitlines() == ["recommended", *expected_dependencies]
    assert [name for name, _ in calls] == ["select", "text"]


def test_install_ps1_parses_when_powershell_is_available() -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    command = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{INSTALL_PS1}', [ref]$null, [ref]$errors); "
        "if ($errors.Count) { exit 1 }"
    )
    subprocess.run([powershell, "-NoProfile", "-Command", command], check=True)
