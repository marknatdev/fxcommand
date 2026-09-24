<#
.SYNOPSIS
  Runs FXCommand against the logged-in MetaTrader 5 terminal and restarts it if it crashes.

.DESCRIPTION
  Used by the "FXCommand" scheduled task (install-autostart.ps1), or run it by hand.
  - BROKER=mt5, one process, one worker (ADR 0001).
  - Output goes to backend\data\logs\fxcommand-YYYYMMDD.log.
  - If the app exits unexpectedly it is started again after 10 seconds. Sessions that were
    running come back as Interrupted (auto-resume only where a Session opted in and the terminal
    is still logged into the same account).
  - Stop it with Ctrl+C, or: Stop-ScheduledTask -TaskName FXCommand
#>
param(
    [string]$Broker = "mt5",
    [string]$Mt5Path = "C:\Program Files\MetaTrader 5\terminal64.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$logs = Join-Path $backend "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

$env:BROKER = $Broker
if ($Broker -eq "mt5" -and (Test-Path $Mt5Path) -and -not $env:MT5_PATH) { $env:MT5_PATH = $Mt5Path }

while ($true) {
    $log = Join-Path $logs ("fxcommand-{0}.log" -f (Get-Date -Format "yyyyMMdd"))
    Add-Content $log ("==== {0} starting FXCommand (BROKER={1}) ====" -f (Get-Date -Format "s"), $Broker)
    Push-Location $backend
    try {
        & uv run --no-sync fxcommand *>> $log
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    Add-Content $log ("==== {0} FXCommand exited with code {1} ====" -f (Get-Date -Format "s"), $code)
    if ($code -eq 0) { break }  # a clean shutdown (Ctrl+C) is not restarted
    Start-Sleep -Seconds 10
}
