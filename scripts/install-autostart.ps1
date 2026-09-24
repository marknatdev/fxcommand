<#
.SYNOPSIS
  Registers a Windows scheduled task that starts FXCommand (BROKER=mt5) when you log on.

.DESCRIPTION
  Run this yourself, once, in a normal PowerShell window:
      powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
  It creates the task "FXCommand" for the current user:
  - trigger: at logon of this user (the MT5 terminal needs your desktop session)
  - action:  scripts\run-fxcommand.ps1 (restarts the app 10 s after a crash)
  - no time limit, runs on battery, one instance only
  The dashboard stays on http://127.0.0.1:8000 (localhost only).
  Remove it with scripts\uninstall-autostart.ps1.
#>
param([string]$TaskName = "FXCommand")

$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run-fxcommand.ps1"
if (-not (Test-Path $runner)) { throw "run-fxcommand.ps1 not found next to this script" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "FXCommand trading engine + dashboard (BROKER=mt5). Logs: backend\data\logs" -Force | Out-Null

Write-Host "Scheduled task '$TaskName' registered. It starts at your next logon."
Write-Host "Start it now:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop it:       Stop-ScheduledTask -TaskName $TaskName"
