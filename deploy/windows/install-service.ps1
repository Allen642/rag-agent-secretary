#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Register scripts/wecom_bot.py as a Windows service with NSSM.

.DESCRIPTION
    Run this script from an elevated PowerShell. It installs (or reinstalls)
    the service, points stdout/stderr at data\runtime\logs, configures the
    process to restart automatically after a crash (5 second delay), and sets
    the service to start automatically at boot.

    The script is idempotent: if the service already exists it is stopped and
    removed before being created again. It never writes secrets; the service
    reads the repository .env through secretary.config.

.PARAMETER ServiceName
    Windows service name. Default: wecom-bot.

.PARAMETER PythonPath
    Python interpreter inside the virtual environment.
    Default: <AppDirectory>\.venv\Scripts\python.exe

.PARAMETER ScriptPath
    Bot entry script. Default: <AppDirectory>\scripts\wecom_bot.py

.PARAMETER AppDirectory
    Repository root, used as the service working directory.
    Default: two levels above this script (the repository root).

.PARAMETER NssmPath
    Full path to nssm.exe. When omitted the script looks for nssm.exe on PATH.

.EXAMPLE
    .\deploy\windows\install-service.ps1 -NssmPath C:\Tools\nssm\nssm.exe
#>

[CmdletBinding()]
param(
    [string]$ServiceName = "wecom-bot",
    [string]$PythonPath,
    [string]$ScriptPath,
    [string]$AppDirectory,
    [string]$NssmPath
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not $AppDirectory) {
    $AppDirectory = $repoRoot
}
$AppDirectory = (Resolve-Path -LiteralPath $AppDirectory).Path

if (-not $PythonPath) {
    $PythonPath = Join-Path $AppDirectory ".venv\Scripts\python.exe"
}
if (-not $ScriptPath) {
    $ScriptPath = Join-Path $AppDirectory "scripts\wecom_bot.py"
}

if (-not $NssmPath) {
    $nssmCommand = Get-Command "nssm.exe" -ErrorAction SilentlyContinue
    if ($nssmCommand) {
        $NssmPath = $nssmCommand.Source
    }
    else {
        throw "nssm.exe not found. Download NSSM and pass -NssmPath C:\Tools\nssm\nssm.exe (see deploy\windows\README.md)."
    }
}

$requiredFiles = @(
    @{ Label = "NSSM"; Path = $NssmPath },
    @{ Label = "Python"; Path = $PythonPath },
    @{ Label = "Bot script"; Path = $ScriptPath }
)
foreach ($item in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $item.Path -PathType Leaf)) {
        throw "$($item.Label) not found: $($item.Path)"
    }
}

$logDir = Join-Path $AppDirectory "data\runtime\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stdoutPath = Join-Path $logDir "$ServiceName.out.log"
$stderrPath = Join-Path $logDir "$ServiceName.err.log"

function Invoke-Nssm {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $NssmPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' already exists; stopping and removing it first."
    if ($existing.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force
        $existing.WaitForStatus("Stopped", "00:00:30")
    }
    Invoke-Nssm remove $ServiceName confirm
}

Write-Host "Installing service '$ServiceName'..."
Invoke-Nssm install $ServiceName $PythonPath $ScriptPath
Invoke-Nssm set $ServiceName AppDirectory $AppDirectory
Invoke-Nssm set $ServiceName AppStdout $stdoutPath
Invoke-Nssm set $ServiceName AppStderr $stderrPath
Invoke-Nssm set $ServiceName AppExit Default Restart
Invoke-Nssm set $ServiceName AppRestartDelay 5000
Invoke-Nssm set $ServiceName Start SERVICE_AUTO_START

Start-Service -Name $ServiceName
$service = Get-Service -Name $ServiceName

Write-Host "Service '$ServiceName' is $($service.Status)."
Write-Host "  Repository: $AppDirectory"
Write-Host "  Python:     $PythonPath"
Write-Host "  Script:     $ScriptPath"
Write-Host "  Stdout log: $stdoutPath"
Write-Host "  Stderr log: $stderrPath"
Write-Host "Health check: .\deploy\tools\healthcheck.ps1"
