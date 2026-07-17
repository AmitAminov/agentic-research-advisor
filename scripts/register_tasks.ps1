# =====================================================================
#  Register the ResearcherWeeklyReproduction scheduled task (created
#  DISABLED).
#
#  Cadence (Amit directive 2026-07-17): reproduction runs are WEEKLY -
#  Sunday 10:00 local time (Asia/Jerusalem machine time). The task runs
#  the full session_pipeline.ps1 cycle (gate -> harvest -> wiki ingest ->
#  reproduce -> webapp -> QA -> report -> git sync -> public sync), which
#  carries its own ExpressVPN fail-closed guard and single-instance lock.
#
#  The task is created DISABLED and stays disabled until deliberately
#  enabled (Enable-ScheduledTask -TaskName 'ResearcherWeeklyReproduction')
#  - consistent with the 2026-07-03 secrets-cleanup policy of keeping the
#  researcher's scheduled tasks off until verified. Re-running this script
#  is idempotent. It also removes the legacy daily task WikiDailyResearcher
#  (superseded by this weekly task).
# =====================================================================
$ErrorActionPreference = 'Stop'
$repo     = Split-Path -Parent $PSScriptRoot
$pipeline = Join-Path $repo "scripts\session_pipeline.ps1"

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$pipeline`" -ReproduceBudgetMin 360"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 10:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
            -DontStopOnIdleEnd

Register-ScheduledTask -TaskName 'ResearcherWeeklyReproduction' `
  -Action $action -Trigger $trigger -Settings $settings `
  -Description 'Weekly (Sunday 10:00): harvest papers (AI/DS/ML/DL), ingest to LLM-wiki, reproduce results, rebuild web app, QA, push to GitHub + public aggregate sync, email report. Gated behind the wiki backlog; ExpressVPN fail-closed guard; single-instance lock.' `
  -Force | Out-Null

# keep it DISABLED until deliberately enabled (2026-07-03 policy)
Disable-ScheduledTask -TaskName 'ResearcherWeeklyReproduction' | Out-Null

# remove the superseded daily task (its 13:00 daily trigger is no longer wanted)
if (Get-ScheduledTask -TaskName 'WikiDailyResearcher' -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName 'WikiDailyResearcher' -Confirm:$false
  Write-Output "Removed legacy daily task WikiDailyResearcher."
}

Write-Output "Registered ResearcherWeeklyReproduction (DISABLED, weekly Sunday 10:00, 12h limit)."
Get-ScheduledTask -TaskName 'ResearcherWeeklyReproduction' | Select-Object TaskName, State | Format-List
