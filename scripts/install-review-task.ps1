<#
.SYNOPSIS
  Registers the weekly Claude Strategy Review as a Windows scheduled task (Saturdays, GOLD is closed).

.DESCRIPTION
  Run this yourself, once, in a normal PowerShell window:
      powershell -ExecutionPolicy Bypass -File scripts\install-review-task.ps1
  It creates the task "FXCommand GOLD review" for the current user:
  - trigger: every Saturday at 10:00 local time (runs later if the PC was off, when you are logged on)
  - action:  scripts\run-gold-review.ps1 (headless Claude Code with a restricted tool set; see that file)
  - at most 3 hours, one instance only
  Requirements: the FXCommand app running on :8000 and the Claude Code CLI logged in for this user.
  Run a review now:  Start-ScheduledTask -TaskName "FXCommand GOLD review"
  Remove it with scripts\uninstall-review-task.ps1.
#>
param(
    [string]$TaskName = "FXCommand GOLD review",
    [string]$At = "10:00",
    [string]$BaseBranch = "main"
)

$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run-gold-review.ps1"
if (-not (Test-Path $runner)) { throw "run-gold-review.ps1 not found next to this script" }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw "the Claude Code CLI ('claude') is not on PATH" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -BaseBranch $BaseBranch"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $At
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Weekly Claude Strategy Review of the GOLD strategies (read-only tools, research CLI, Challengers and PRs only). Logs: backend\data\logs\gold-review-*.log" -Force | Out-Null

Write-Host "Scheduled task '$TaskName' registered: Saturdays at $At."
Write-Host "Run it now:  Start-ScheduledTask -TaskName '$TaskName'"
