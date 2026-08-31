$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$UvInstallUrl = "https://astral.sh/uv/install.ps1"
$BubPackage = "bub@latest"

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host $Message
}

function Invoke-Uv {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $script:UvPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "uv $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$UvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
if ($null -ne $UvCommand) {
    $script:UvPath = $UvCommand.Source
    Write-Step "Using uv at $script:UvPath"
}
else {
    $UvInstallDir = Join-Path $HOME ".local\bin"
    Write-Step "Installing uv..."

    $env:UV_INSTALL_DIR = $UvInstallDir
    $UvInstaller = Invoke-RestMethod -Uri $UvInstallUrl -UseBasicParsing
    Invoke-Expression $UvInstaller

    $script:UvPath = Join-Path $UvInstallDir "uv.exe"
    if (-not (Test-Path -LiteralPath $script:UvPath -PathType Leaf)) {
        throw "uv was installed, but $script:UvPath does not exist"
    }
}

Write-Step "Installing Bub..."
Invoke-Uv -Arguments @("tool", "install", $BubPackage)

# Updating the persistent PATH is best-effort because some PowerShell hosts do
# not expose a supported profile. The executable directory is printed below.
& $script:UvPath tool update-shell
if ($LASTEXITCODE -ne 0) {
    Write-Warning "uv could not update your PowerShell profile"
}

$ToolBinOutput = & $script:UvPath tool dir --bin
if ($LASTEXITCODE -ne 0) {
    throw "uv tool dir --bin failed with exit code $LASTEXITCODE"
}
$ToolBin = ($ToolBinOutput | Select-Object -Last 1).Trim()

$PathEntries = $env:PATH -split [IO.Path]::PathSeparator
if ($ToolBin -notin $PathEntries) {
    $env:PATH = "$ToolBin$([IO.Path]::PathSeparator)$env:PATH"
}

Write-Host
Write-Step "Bub was installed successfully."
Write-Step "Executable directory: $ToolBin"
Write-Step "Restart your shell, then run: bub --help"
