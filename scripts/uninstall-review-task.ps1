<#
.SYNOPSIS
  Removes the weekly Claude Strategy Review scheduled task. Its worktree (..\fxcommand-review) is left
  in place; remove it with: git worktree remove ..\fxcommand-review
#>
param([string]$TaskName = "FXCommand GOLD review")

$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task '$TaskName' removed."
} else {
    Write-Host "No scheduled task named '$TaskName'."
}
