$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction Stop).Source
$script = Join-Path $project "stock_radar_scan.py"
$taskName = "ForwardVolScanner-StockRadar"

# New York close maps to 06:00, 07:00, or 08:00 the next day in Melbourne,
# depending on the two markets' daylight-saving transitions.  The due-only
# guard uses America/New_York and makes the other two launches harmless no-ops.
$days = @("Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
$triggers = @(
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days -At "06:15"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days -At "07:15"
    New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $days -At "08:15"
)
$arguments = '"' + $script + '" --cadence auto --source yf --due-only'
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $project
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "Build daily and Friday Stock Opportunity Radar watchlists after the New York close." -Force | Out-Null

Write-Host "Installed $taskName. It checks the New York close at 06:15/07:15/08:15 Melbourne time."
