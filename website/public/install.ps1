$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$UvInstallUrl = "https://astral.sh/uv/install.ps1"
$DefaultPresetsUrl = "https://bub.build/presets.json"
$BubPackage = "bub"
$BubPython = "3.12"
$InquirerPackage = "inquirer-textual==0.6.1"
$script:UvPath = $null
$script:Interactive = $false
$script:RequestedPreset = $null
$script:ExtraDependencies = [System.Collections.Generic.List[string]]::new()
$script:UseColor = $false

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)

    if ($script:UseColor) {
        Write-Host "==>" -ForegroundColor Cyan -NoNewline
        Write-Host " $Message"
    }
    else {
        Write-Host $Message
    }
}

function Show-InstallerHelp {
    Write-Host @"
Install Bub and optional plugin presets.

Usage:
  install.ps1 [--preset PRESET] [--dependency SPEC]...

Options:
  --preset PRESET       Select a preset without prompting.
  --dependency SPEC     Install an extra plugin dependency. Repeatable.
  --plugin SPEC         Alias for --dependency.
  -h, --help            Show this help.

Examples:
  powershell -ExecutionPolicy ByPass -c "irm https://bub.build/install.ps1 | iex"
  powershell -ExecutionPolicy ByPass -c "& ([scriptblock]::Create((irm https://bub.build/install.ps1))) --preset recommended"
"@
}

function Read-InstallerArguments {
    param([string[]]$Arguments)

    for ($Index = 0; $Index -lt $Arguments.Count; $Index++) {
        $Argument = $Arguments[$Index]
        switch -Regex ($Argument) {
            '^--preset$' {
                if ($null -ne $script:RequestedPreset) {
                    throw "--preset may only be specified once"
                }
                $Index++
                if ($Index -ge $Arguments.Count) {
                    throw "--preset requires a value"
                }
                $script:RequestedPreset = $Arguments[$Index]
                break
            }
            '^--preset=' {
                if ($null -ne $script:RequestedPreset) {
                    throw "--preset may only be specified once"
                }
                $script:RequestedPreset = $Argument.Substring("--preset=".Length)
                if ([string]::IsNullOrEmpty($script:RequestedPreset)) {
                    throw "--preset requires a value"
                }
                break
            }
            '^--(?:dependency|plugin)$' {
                $Option = $Argument
                $Index++
                if ($Index -ge $Arguments.Count) {
                    throw "$Option requires a value"
                }
                $script:ExtraDependencies.Add($Arguments[$Index])
                break
            }
            '^--(?:dependency|plugin)=' {
                $Dependency = $Argument.Substring($Argument.IndexOf('=') + 1)
                if ([string]::IsNullOrEmpty($Dependency)) {
                    throw "$($Argument.Split('=')[0]) requires a value"
                }
                $script:ExtraDependencies.Add($Dependency)
                break
            }
            '^(?:-h|--help)$' {
                Show-InstallerHelp
                exit 0
            }
            default {
                throw "unknown argument: $Argument"
            }
        }
    }
}

function Invoke-Uv {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $script:UvPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "uv $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Bub {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $script:BubPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "bub $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Install-Uv {
    $UvInstallDir = Join-Path $HOME ".local\bin"
    Write-Step "Installing uv"

    $env:UV_INSTALL_DIR = $UvInstallDir
    $UvInstaller = Invoke-RestMethod -Uri $UvInstallUrl -UseBasicParsing
    Invoke-Expression $UvInstaller

    $script:UvPath = Join-Path $UvInstallDir "uv.exe"
    if (-not (Test-Path -LiteralPath $script:UvPath -PathType Leaf)) {
        throw "uv was installed, but $script:UvPath does not exist"
    }
}

function Set-BubExecutableLink {
    param(
        [Parameter(Mandatory = $true)][string]$TargetPath,
        [Parameter(Mandatory = $true)][string]$LinkPath
    )

    $LinkDirectory = Split-Path -Parent $LinkPath
    [IO.Directory]::CreateDirectory($LinkDirectory) | Out-Null

    $ExistingItem = Get-Item -LiteralPath $LinkPath -Force -ErrorAction SilentlyContinue
    if ($null -ne $ExistingItem) {
        if ($ExistingItem.PSIsContainer) {
            throw "$LinkPath already exists and is a directory"
        }
        Remove-Item -LiteralPath $LinkPath -Force
    }

    New-Item -ItemType HardLink -Path $LinkPath -Target $TargetPath | Out-Null
}

function Get-PresetResolution {
    param(
        [Parameter(Mandatory = $true)][string]$CatalogPath,
        [Parameter(Mandatory = $true)][string]$ResolutionPath
    )

    $Mode = if ($script:Interactive) { "interactive" } else { "noninteractive" }
    $RequestedPresetValue = if ($null -eq $script:RequestedPreset) { "" } else { $script:RequestedPreset }
    $ResolverArguments = @($CatalogPath, $Mode, $RequestedPresetValue, $ResolutionPath)
    $ResolverArguments += @($script:ExtraDependencies)
    $UvArguments = @("run", "--no-project")
    if ($script:Interactive) {
        $UvArguments += @("--with", $InquirerPackage)
    }
    $UvArguments += @("python", "-")

    $EmbeddedPython = @'
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def abort(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate_dependency(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        abort(f"{context} must be a non-empty string")
    if value.startswith("-") or any(character in value for character in "\r\n\0"):
        abort(f"{context} is not a safe package specification: {value!r}")
    return value


def load_catalog(path: str) -> list[dict[str, object]]:  # noqa: C901
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        abort(f"could not read preset catalog: {error}")

    if not isinstance(data, dict) or data.get("schema_version") != 1:
        abort("preset catalog must use schema_version 1")
    presets = data.get("presets")
    if not isinstance(presets, list) or not presets:
        abort("preset catalog must contain a non-empty presets list")

    names: set[str] = set()
    default_count = 0
    for index, preset in enumerate(presets):
        context = f"presets[{index}]"
        if not isinstance(preset, dict):
            abort(f"{context} must be an object")
        name = preset.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            abort(f"{context}.name must be a lowercase kebab-case string")
        if name in names:
            abort(f"preset name is duplicated: {name}")
        names.add(name)
        for field in ("title", "description"):
            if not isinstance(preset.get(field), str) or not preset[field]:
                abort(f"{context}.{field} must be a non-empty string")
        dependencies = preset.get("dependencies")
        if not isinstance(dependencies, list):
            abort(f"{context}.dependencies must be a list")
        for dependency_index, dependency in enumerate(dependencies):
            validate_dependency(dependency, f"{context}.dependencies[{dependency_index}]")
        if not isinstance(preset.get("default"), bool):
            abort(f"{context}.default must be a boolean")
        default_count += int(preset["default"])

    if default_count != 1:
        abort("preset catalog must contain exactly one default preset")
    return presets


def attach_terminal() -> None:
    if os.name == "nt":
        input_stream = open("CONIN$", encoding="utf-8")  # noqa: SIM115
        output_stream = open("CONOUT$", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    else:
        input_stream = open("/dev/tty", encoding="utf-8")  # noqa: SIM115
        output_stream = open("/dev/tty", "w", encoding="utf-8", buffering=1)  # noqa: SIM115

    # Textual reads the original standard streams directly.
    sys.stdin = sys.__stdin__ = input_stream
    sys.stdout = sys.__stdout__ = output_stream
    sys.stderr = sys.__stderr__ = output_stream


def choose_preset(presets: list[dict[str, object]]) -> tuple[dict[str, object], list[str]]:
    attach_terminal()

    from inquirer_textual import prompts
    from inquirer_textual.common.Choice import Choice
    from inquirer_textual.common.PromptSettings import PromptSettings

    choices: list[Choice] = []
    default_choice: Choice | None = None
    for preset in presets:
        choice = Choice(f"{preset['title']} — {preset['description']}", data=preset["name"])
        choices.append(choice)
        if preset["default"]:
            default_choice = choice

    settings = PromptSettings(mandatory=True, mouse=True)
    answer = prompts.select("Choose a Bub plugin preset:", choices, default=default_choice, settings=settings).value
    if not isinstance(answer, Choice) or not isinstance(answer.data, str):
        abort("no preset was selected")
    selected = next(preset for preset in presets if preset["name"] == answer.data)

    extra_answer = prompts.text(
        "Additional plugins (space-separated, optional):",
        settings=PromptSettings(mouse=True),
    ).value
    try:
        extras = shlex.split(extra_answer or "")
    except ValueError as error:
        abort(f"invalid plugin list: {error}")
    return selected, extras


def main() -> None:
    catalog_path, mode, requested_preset, resolution_path, *extras = sys.argv[1:]
    presets = load_catalog(catalog_path)
    if mode == "interactive":
        selected, prompted_extras = choose_preset(presets)
        extras.extend(prompted_extras)
    else:
        selected = next((preset for preset in presets if preset["name"] == requested_preset), None)
        if selected is None:
            available = ", ".join(str(preset["name"]) for preset in presets)
            abort(f"unknown preset {requested_preset!r}; available presets: {available}")

    dependencies: list[str] = []
    for index, dependency in enumerate([*selected["dependencies"], *extras]):
        value = validate_dependency(dependency, f"dependency[{index}]")
        if value not in dependencies:
            dependencies.append(value)

    resolution = "\n".join([str(selected["name"]), *dependencies]) + "\n"
    try:
        Path(resolution_path).write_text(resolution, encoding="utf-8")
    except OSError as error:
        abort(f"could not write preset resolution: {error}")


if __name__ == "__main__":
    main()
'@

    $EmbeddedPython | & $script:UvPath @UvArguments @ResolverArguments
    if ($LASTEXITCODE -ne 0) {
        throw "failed to resolve Bub preset"
    }
}

Read-InstallerArguments -Arguments $args
$script:Interactive = $null -eq $script:RequestedPreset
if ($script:Interactive -and (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected)) {
    throw "no interactive terminal is available; pass --preset PRESET"
}
$script:UseColor = $script:Interactive -and -not $env:NO_COLOR -and $env:TERM -ne "dumb"

if ([string]::IsNullOrEmpty($HOME)) {
    throw "HOME is not set"
}

$UvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
if ($null -ne $UvCommand) {
    $script:UvPath = $UvCommand.Source
    Write-Step "Using uv at $script:UvPath"
}
else {
    Install-Uv
}

$PresetFile = [IO.Path]::GetTempFileName()
$ResolutionFile = [IO.Path]::GetTempFileName()
try {
    $PresetsUrl = if ($env:BUB_INSTALLER_PRESETS_URL) { $env:BUB_INSTALLER_PRESETS_URL } else { $DefaultPresetsUrl }
    Write-Step "Loading plugin presets"
    Invoke-WebRequest -Uri $PresetsUrl -UseBasicParsing -OutFile $PresetFile
    Get-PresetResolution -CatalogPath $PresetFile -ResolutionPath $ResolutionFile
    $ResolvedLines = @(Get-Content -LiteralPath $ResolutionFile)
    if ($ResolvedLines.Count -lt 1) {
        throw "preset resolver returned no selection"
    }
}
finally {
    Remove-Item -LiteralPath $PresetFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ResolutionFile -Force -ErrorAction SilentlyContinue
}

$SelectedPreset = $ResolvedLines[0]
$Dependencies = @($ResolvedLines | Select-Object -Skip 1)

$BubRoot = Join-Path $HOME ".bub"
$VenvPath = Join-Path $BubRoot ".venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$VenvBub = Join-Path $VenvPath "Scripts\bub.exe"
$ExecutableDirectory = Join-Path $HOME ".local\bin"
$script:BubPath = Join-Path $ExecutableDirectory "bub.exe"

Write-Step "Creating Bub virtual environment"
[IO.Directory]::CreateDirectory($BubRoot) | Out-Null
Invoke-Uv -Arguments @("venv", "--python", $BubPython, "--allow-existing", $VenvPath)

Write-Step "Installing Bub"
Invoke-Uv -Arguments @("pip", "install", "--python", $VenvPython, "--upgrade", $BubPackage)
if (-not (Test-Path -LiteralPath $VenvBub -PathType Leaf)) {
    throw "Bub was installed, but $VenvBub does not exist"
}
Set-BubExecutableLink -TargetPath $VenvBub -LinkPath $script:BubPath

$PathEntries = $env:PATH -split [IO.Path]::PathSeparator
if ($ExecutableDirectory -notin $PathEntries) {
    $env:PATH = "$ExecutableDirectory$([IO.Path]::PathSeparator)$env:PATH"
}

if ($Dependencies.Count -gt 0) {
    Write-Step "Installing plugins for preset $SelectedPreset"
    Invoke-Bub -Arguments (@("install", "--") + $Dependencies)
}

if ($script:Interactive) {
    Write-Step "Starting Bub onboarding"
    Invoke-Bub -Arguments @("onboard")
}

Write-Host
if ($script:UseColor) {
    Write-Host "Bub was installed successfully." -ForegroundColor Green
}
else {
    Write-Host "Bub was installed successfully."
}
Write-Host "Preset: $SelectedPreset"
Write-Host "Virtual environment: $VenvPath"
Write-Host "Executable link: $script:BubPath"
Write-Host "Ensure $ExecutableDirectory is on PATH, then run: bub --help"
