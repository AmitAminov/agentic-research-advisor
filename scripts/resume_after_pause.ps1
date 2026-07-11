# =====================================================================
#  RESUME AFTER PAUSE
#
#  Fired ONCE by the one-time scheduled task "ResearcherResume3Day" to
#  undo the 2026-07-07 "cancel all runs for 3 days" pause. It:
#    1. re-enables the WikiDailyResearcher scheduled task (02:30 daily
#       daily_pipeline.ps1 path),
#    2. clears the session-runner stop flag,
#    3. relaunches the session-runner.ps1 daemon (6h session_pipeline path;
#       its own single-instance guard prevents duplicates),
#    4. removes its own one-time trigger task so it never fires again.
#
#  Idempotent and side-effect-safe: it starts schedulers, it does not run
#  a pipeline cycle itself (no harvest/reproduce/email/push here).
# =====================================================================
$ErrorActionPreference = 'Continue'
$base   = 'C:\Users\ADMIN\Agentic_Projects\AI_DS_ML_DL_Researcher\AI_DS_ML_DL\wiki\.ingest'
$runner = Join-Path $base 'session-runner.ps1'
$stop   = Join-Path $base 'session-runner.stop'
$log    = Join-Path $base 'logs\resume.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function RLog($m){ ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) | Out-File $log -Append -Encoding utf8 }

RLog "RESUME START (undo 3-day pause)."

# 1. re-enable the daily scheduled-task trigger path
try { Enable-ScheduledTask -TaskName 'WikiDailyResearcher' -EA Stop | Out-Null; RLog "WikiDailyResearcher re-enabled." }
catch { RLog "WikiDailyResearcher enable ERROR: $_" }

# 2. clear the stop flag (session-runner also clears it on startup)
if (Test-Path $stop) { Remove-Item $stop -Force -EA SilentlyContinue; RLog "cleared session-runner.stop." }

# 3. relaunch the session-runner daemon (detached, hidden); guard blocks dupes
if (Test-Path $runner) {
  Start-Process powershell -WindowStyle Hidden -ArgumentList `
    '-NoProfile','-ExecutionPolicy','Bypass','-File',$runner
  RLog "session-runner.ps1 relaunched (detached)."
} else { RLog "FATAL: session-runner.ps1 not found at $runner." }

# 4. self-remove the one-time resume task so it never fires again
try { Unregister-ScheduledTask -TaskName 'ResearcherResume3Day' -Confirm:$false -EA Stop; RLog "removed one-time task ResearcherResume3Day." }
catch { RLog "ResearcherResume3Day removal note: $_" }

RLog "RESUME DONE."
