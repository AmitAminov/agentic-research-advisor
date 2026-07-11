# =====================================================================
#  Register the WikiDailyResearcher scheduled task (created DISABLED).
#  It stays disabled until the hourly wiki ingest drains the backlog and
#  activate_daily.ps1 enables it. Re-running this is idempotent.
# =====================================================================
$ErrorActionPreference = 'Stop'
$repo    = Split-Path -Parent $PSScriptRoot
$daily   = Join-Path $repo "scripts\daily_pipeline.ps1"

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$daily`""
$trigger = New-ScheduledTaskTrigger -Daily -At 2:30AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
            -DontStopOnIdleEnd

Register-ScheduledTask -TaskName 'WikiDailyResearcher' `
  -Action $action -Trigger $trigger -Settings $settings `
  -Description 'Daily: harvest top-30 last-month papers (AI/DS/ML/DL), ingest to LLM-wiki, reproduce results, rebuild web app, push to GitHub, email report. Gated until the hourly wiki backlog is cleared.' `
  -Force | Out-Null

# keep it disabled until the hourly ingest hands off
Disable-ScheduledTask -TaskName 'WikiDailyResearcher' | Out-Null
Write-Output "Registered WikiDailyResearcher (disabled, daily 02:30, 12h limit)."
Get-ScheduledTask -TaskName 'WikiDailyResearcher' | Select-Object TaskName, State | Format-List
