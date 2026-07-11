# =====================================================================
#  GATE TRANSITION: 20-min wiki ingest loop  ->  ~6h session pipeline.
#
#  Called automatically by parallel-ingest.ps1 (and run-ingest.ps1) once the
#  wiki backlog is fully drained, but can also be run manually. Idempotent.
#
#  Because Task Scheduler cannot launch processes into this interactive,
#  locked-desktop session (see runner-loop.ps1 header), the live cadence is
#  driven by detached long-lived loops, not scheduled tasks. So the real
#  transition is:
#    1. STOP the 20-minute wiki runner-loop  (drop wiki\.ingest\runner.stop)
#    2. LAUNCH session-runner.ps1 detached    (~4x/day session_pipeline.ps1)
#  The scheduled-task toggling is kept as harmless best-effort for any
#  environment where Task Scheduler does work.
# =====================================================================
param(
  [switch]$RunImmediately,           # have session-runner fire one cycle at startup
  [double]$IntervalHours = 6
)
$ErrorActionPreference = 'Continue'

$repo   = Split-Path -Parent $PSScriptRoot
$ingest = Join-Path $repo "AI_DS_ML_DL\wiki\.ingest"
$marker = Join-Path $repo "state\.transitioned"
$wikiStop        = Join-Path $ingest "runner.stop"
$wikiPid         = Join-Path $ingest "runner.pid"
$sessionRunner   = Join-Path $ingest "session-runner.ps1"
$sessionPid      = Join-Path $ingest "session-runner.pid"

function TLog($m){ Write-Output ("[transition] {0}" -f $m) }

try {
  # ---- 1. STOP the 20-minute wiki runner-loop ------------------------
  Set-Content -Path $wikiStop -Value (Get-Date -Format o) -Encoding utf8
  TLog "wrote wiki runner stop flag -> $wikiStop"
  if (Test-Path $wikiPid) {
    $old = (Get-Content $wikiPid -EA SilentlyContinue | Select-Object -First 1)
    if ($old -and (Get-Process -Id $old -EA SilentlyContinue)) {
      TLog "wiki runner (pid $old) still alive; it will exit at its next stop-flag check (<=30s)."
    } else { TLog "no live wiki runner process (pid file stale or gone)." }
  }

  # ---- 2. best-effort scheduled-task toggle (legacy / if Task Scheduler works) ----
  foreach ($tn in 'WikiHourlyIngest','WikiParallelIngest') {
    if (Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue) {
      try { Disable-ScheduledTask -TaskName $tn -ErrorAction Stop | Out-Null; TLog "$tn DISABLED" } catch { TLog "could not disable ${tn}: $_" }
    }
  }

  # ---- 3. LAUNCH the session-runner detached (single-instance guarded) ----
  if (-not (Test-Path $sessionRunner)) {
    TLog "ERROR: session-runner.ps1 not found at $sessionRunner; cannot start session cadence."
  } else {
    $alreadyUp = $false
    if (Test-Path $sessionPid) {
      $sp = (Get-Content $sessionPid -EA SilentlyContinue | Select-Object -First 1)
      if ($sp -and (Get-Process -Id $sp -EA SilentlyContinue)) { $alreadyUp = $true; TLog "session-runner already running (pid $sp); not relaunching." }
    }
    if (-not $alreadyUp) {
      # clear any stale stop flag so the fresh runner does not self-exit
      $srStop = Join-Path $ingest "session-runner.stop"
      if (Test-Path $srStop) { Remove-Item $srStop -Force -EA SilentlyContinue }
      $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File', $sessionRunner, '-IntervalHours', "$IntervalHours")
      if ($RunImmediately) { $argList += '-RunImmediately' }
      Start-Process powershell -WindowStyle Hidden -ArgumentList $argList | Out-Null
      TLog "launched session-runner.ps1 detached (interval=${IntervalHours}h, RunImmediately=$RunImmediately)."
    }
  }

  # ---- 4. best-effort: enable a daily scheduled task if present ------
  if (Get-ScheduledTask -TaskName 'WikiDailyResearcher' -ErrorAction SilentlyContinue) {
    try { Enable-ScheduledTask -TaskName 'WikiDailyResearcher' -ErrorAction Stop | Out-Null; TLog "WikiDailyResearcher ENABLED" } catch { TLog "could not enable WikiDailyResearcher: $_" }
  }

  # ---- 5. marker -----------------------------------------------------
  New-Item -ItemType Directory -Force -Path (Split-Path $marker) | Out-Null
  Set-Content -Path $marker -Value (Get-Date -Format s) -Encoding utf8
  TLog "done; marker written -> $marker"
} catch {
  TLog "FAILED: $_"
}
