# =====================================================================
#  DAILY RESEARCH PIPELINE  (run by Task Scheduler: WikiDailyResearcher)
#
#  Order:
#   0. (gate) only proceed once the hourly wiki backlog is fully drained
#   1. harvest top-30 papers of the last month across AI/DS/ML/DL -> raw/Research
#   2. ingest the new papers into the LLM-wiki (reuses run-ingest.ps1)
#   3. reproduce papers (bounded wall-clock; backfills oldest-first over days)
#   4. rebuild the web app
#   5. commit + push the researcher repo
#   6. email the daily progress report
#
#  Manual run:  powershell -File daily_pipeline.ps1 -Force
#  -Force skips the gate (for testing before the backlog is cleared).
# =====================================================================
param(
  [switch]$Force,
  [int]$ReproduceBudgetMin = 300,   # wall-clock budget for the reproduction phase
  [int]$WikiBatch = 40              # ingest at least the day's harvest into the wiki
)
$ErrorActionPreference = 'Continue'

# paths are derived from this script's location; config.json may override
$scripts = $PSScriptRoot
$repo    = Split-Path -Parent $scripts
$cfg     = Join-Path $repo "config.json"
$conf    = if (Test-Path $cfg) { Get-Content $cfg -Raw | ConvertFrom-Json } else { $null }
$wiki    = if ($conf -and $conf.paths.wiki_project) { $conf.paths.wiki_project } else { Join-Path $repo "AI_DS_ML_DL" }
$py      = if ($conf -and $conf.paths.python) { $conf.paths.python }
           elseif (Test-Path (Join-Path $repo ".venv\Scripts\python.exe")) { Join-Path $repo ".venv\Scripts\python.exe" }
           else { "python" }
$logsDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$stamp   = Get-Date -Format "yyyy-MM-dd_HHmmss"
$log     = Join-Path $logsDir "daily-$stamp.log"
function Log($m){ $l="[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"),$m; Add-Content $log $l -Encoding utf8; Write-Output $l }
function Ensure-VpnForInternet {
  $vpnGuard = Join-Path $scripts "expressvpn_mcp_guard.ps1"
  if (-not (Test-Path $vpnGuard)) { Log "FATAL: ExpressVPN MCP guard missing at $vpnGuard"; exit 3 }
  Log "VPN guard: ensuring ExpressVPN MCP connection before internet work."
  & powershell -NoProfile -ExecutionPolicy Bypass -File $vpnGuard 2>&1 | ForEach-Object { Log "  [vpn] $_" }
  if ($LASTEXITCODE -ne 0) { Log "FATAL: ExpressVPN MCP guard failed; refusing to run internet pipeline steps."; exit 3 }
}

if (-not (Test-Path $cfg)) { Log "FATAL: config.json missing (copy config.example.json). Exiting."; exit 2 }

# ---- single-instance guard (SHARED with session_pipeline.ps1) --------
#  Same lock file as session_pipeline so the two orchestrators can never
#  overlap each other: the 02:30 daily firing while a ~4h session cycle is
#  still reproducing would double every provider's request rate (the
#  2026-07-03 overload; NETWORK_ETIQUETTE.md rule 8). A lock whose PID is
#  not a live powershell is stale (crashed cycle) and is reclaimed.
$lockFile = Join-Path $logsDir "session_pipeline.lock"
if (Test-Path $lockFile) {
  $oldPid = (Get-Content $lockFile -EA SilentlyContinue | Select-Object -First 1)
  if ($oldPid -match '^\d+$') {
    $alive = Get-Process -Id ([int]$oldPid) -EA SilentlyContinue
    if ($alive -and $alive.ProcessName -eq 'powershell') {
      Log "ANOTHER PIPELINE CYCLE ALREADY RUNNING (pid=$oldPid). Exiting 0 to avoid overlapping request load."
      exit 0
    }
    Log "Stale lock (pid=$oldPid not a live powershell) -> reclaiming."
  }
}
"$PID" | Out-File -FilePath $lockFile -Encoding ascii -Force
Log "single-instance lock claimed (pid=$PID, shared with session_pipeline)."

# ---- 0. gate ---------------------------------------------------------
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scripts "check_gate.ps1") -WikiProject $wiki | Tee-Object -Variable gateOut | Out-Null
$gateOpen = ($LASTEXITCODE -eq 0)
Log "gate check: $gateOut (open=$gateOpen)"
if (-not $gateOpen -and -not $Force) {
  Log "Gate CLOSED: wiki backlog not yet drained. Daily pipeline standing by (hourly ingest still running). Exiting."
  Remove-Item $lockFile -Force -EA SilentlyContinue
  exit 0
}

Ensure-VpnForInternet

# ---- 1. harvest ------------------------------------------------------
Log "STEP 1: harvest top-30 last-month papers (AI/DS/ML/DL)"
& $py (Join-Path $scripts "fetch_papers.py") --config $cfg 2>&1 | ForEach-Object { Log "  [fetch] $_" }

# ---- 2. ingest new papers into the wiki ------------------------------
Log "STEP 2: ingest new papers into LLM-wiki (batch=$WikiBatch)"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $wiki "wiki\.ingest\run-ingest.ps1") -Batch $WikiBatch 2>&1 | ForEach-Object { Log "  [wiki] $_" }

# ---- 3. reproduce ----------------------------------------------------
Log "STEP 3: reproduce papers (budget ${ReproduceBudgetMin}m, oldest-first backfill)"
& $py (Join-Path $scripts "reproduce.py") --config $cfg --backfill --deadline-minutes $ReproduceBudgetMin 2>&1 | ForEach-Object { Log "  [repro] $_" }

# ---- 4. webapp -------------------------------------------------------
Log "STEP 4: rebuild web app"
& $py (Join-Path $scripts "build_webapp.py") --repo $repo 2>&1 | ForEach-Object { Log "  [web] $_" }

# ---- 5. commit + push ------------------------------------------------
Log "STEP 5: commit + push"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scripts "git_autopush.ps1") -Repo $repo 2>&1 | ForEach-Object { Log "  [git] $_" }

# ---- 6. email --------------------------------------------------------
Log "STEP 6: email daily report"
& $py (Join-Path $scripts "send_report.py") --config $cfg 2>&1 | ForEach-Object { Log "  [mail] $_" }

Log "DAILY PIPELINE COMPLETE."
Remove-Item $lockFile -Force -EA SilentlyContinue
