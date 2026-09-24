<#
.SYNOPSIS
  Removes the "FXCommand" scheduled task created by install-autostart.ps1 (stops it first).
#>
param([string]$TaskName = "FXCommand")

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "No scheduled task named '$TaskName'."
    exit 0
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Scheduled task '$TaskName' removed."
