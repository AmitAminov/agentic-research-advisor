# =====================================================================
#  SESSION RESEARCH PIPELINE  (GATED full 5-component cycle)
#
#  This is the orchestrator fired once/WEEK - Sunday @ 10:00 local time -
#  by the ResearcherWeeklyReproduction scheduled task (or the detached
#  session-runner.ps1 fallback daemon). Amit directive 2026-07-17: the
#  reproduction cadence moved from daily to weekly. It runs
#  the complete research loop, but ONLY once the LLM-wiki ingest backlog
#  is fully drained (the gate). While the wiki still has new/unclassified
#  or pending-in-scope papers, this pipeline stands by and exits 0 so the
#  20-minute wiki runner-loop keeps priority.
#
#  Order:
#    0. GATE       -- check_gate.ps1; proceed only when new/unclassified==0
#                     AND pending-in-scope==0, else log 'standing by', exit 0
#    1. fetch      -- fetch_papers.py: 30 last-month + 3 last-6h  -> state/harvests
#    2. ingest     -- parallel-ingest.ps1: ingest new papers into the LLM-wiki
#    3. reproduce  -- reproduce.py --backfill (bounded wall-clock)
#    4. manim      -- make_manim.py --backfill (safety net; reproduce also emits it)
#    5. webapp     -- build_webapp.py: rebuild the static reproduction site
#    6. qa         -- qa_agent.py fix loop (skipped gracefully if absent)
#    7. sync       -- github_sync.py commit_and_push  (token NEVER printed)
#    8. report     -- send_report.py: unified PDF email
#
#  Manual run:  powershell -File session_pipeline.ps1 -Force
#    -Force skips the gate (testing only).
#
#  Constraints: Windows, CPU-only, Python 3.10 (.venv). The reproduction
#  harness scales GPU/huge-data papers down faithfully and records the
#  deviations in each paper's summary.md. The GitHub token is read by
#  github_sync.py from its token_file and is never printed or committed.
# =====================================================================
param(
  [switch]$Force,
  [int]$ReproduceBudgetMin = 360,   # wall-clock budget for the reproduction phase (~6h fits the weekly Sunday cadence)
  [int]$IngestWorkers = 2,          # concurrent wiki-ingest claude workers (lowered 4->2, Amit 2026-07-12: so wiki-ingest + reproduction don't both draw heavy concurrent quota)
  [int]$IngestPerWorker = 25,
  [int]$ManimTimeout = 300,
  [int]$QaMaxIterations = 3
)
$ErrorActionPreference = 'Continue'

# ---- paths (derived from this script's location; config.json overrides) ----
$scripts = $PSScriptRoot
$repo    = Split-Path -Parent $scripts
$cfg     = Join-Path $repo "config.json"

if (-not (Test-Path $cfg)) { Write-Output "FATAL: config.json missing (copy config.example.json). Exiting."; exit 2 }
$conf = Get-Content $cfg -Raw | ConvertFrom-Json
$wiki   = if ($conf.paths.wiki_project) { $conf.paths.wiki_project } else { Join-Path $repo "AI_DS_ML_DL" }
$ingest = Join-Path $wiki "wiki\.ingest"
$py     = if ($conf.paths.python) { $conf.paths.python }
          elseif (Test-Path (Join-Path $repo ".venv\Scripts\python.exe")) { Join-Path $repo ".venv\Scripts\python.exe" }
          else { "python" }
if (($py -ne "python") -and -not (Test-Path $py)) { Write-Output "FATAL: python interpreter not found at $py. Exiting."; exit 2 }

$logsDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$log   = Join-Path $logsDir "session-$stamp.log"
function Log($m){ $l = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m; Add-Content -Path $log -Value $l -Encoding utf8; Write-Output $l }
function Ensure-VpnForInternet {
  $vpnGuard = Join-Path $scripts "expressvpn_mcp_guard.ps1"
  if (-not (Test-Path $vpnGuard)) { Log "FATAL: ExpressVPN MCP guard missing at $vpnGuard"; exit 3 }
  Log "VPN guard: ensuring ExpressVPN MCP connection before internet work."
  & powershell -NoProfile -ExecutionPolicy Bypass -File $vpnGuard 2>&1 | ForEach-Object { Log "  [vpn] $_" }
  if ($LASTEXITCODE -ne 0) { Log "FATAL: ExpressVPN MCP guard failed; refusing to run internet pipeline steps."; exit 3 }
}

Log "SESSION PIPELINE START (force=$Force) python=$py"

# ---- single-instance guard -------------------------------------------
#  Refuse to start if another session_pipeline is already running. This is
#  what prevents the overload seen on 2026-07-03, where a forced cycle
#  overran and the 6-hourly runner fired a SECOND cycle on top of it (two
#  cycles => 17+ worker processes => machine saturated). Applies even with
#  -Force: overlap is never wanted. A lock whose PID is dead (killed cycle)
#  is auto-reclaimed, so a crash cannot wedge the pipeline shut.
$lockFile = Join-Path $logsDir "session_pipeline.lock"
if (Test-Path $lockFile) {
  $oldPid = (Get-Content $lockFile -EA SilentlyContinue | Select-Object -First 1)
  if ($oldPid -match '^\d+$') {
    $alive = Get-Process -Id ([int]$oldPid) -EA SilentlyContinue
    if ($alive -and $alive.ProcessName -eq 'powershell') {
      Log "ANOTHER CYCLE ALREADY RUNNING (pid=$oldPid). Exiting 0 to avoid overlap."
      exit 0
    }
    Log "Stale lock (pid=$oldPid not a live powershell) -> reclaiming."
  }
}
"$PID" | Out-File -FilePath $lockFile -Encoding ascii -Force
Log "single-instance lock claimed (pid=$PID)."

# ---- heavy-dataset/model cleanup: RETIRED (cloud storage) ----
#  Heavy datasets/models now live in the cloud registry (gdrive:ML_MODELS + per-project
#  folders) and are pulled on demand; with ~5 TB of Drive, local disk is no longer a
#  constraint, so the disk-hygiene sweep (cleanup_heavy_datasets.ps1) is retired here.
#  The standalone ResearcherDatasetCleanup scheduled task is disabled. See docs/CLOUD_ASSETS.md.

Ensure-VpnForInternet

# ---- 0. GATE ---------------------------------------------------------
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scripts "check_gate.ps1") -WikiProject $wiki 2>&1 | Tee-Object -Variable gateOut | Out-Null
$gateOpen = ($LASTEXITCODE -eq 0)
Log "STEP 0 gate check: $gateOut (open=$gateOpen)"

# Self-heal against a wedged gate. The gate is closed while the wiki has
# new/unclassified papers. Normally the standalone 20-min wiki runner-loop
# drains them, but if that daemon dies the backlog never clears and this
# pipeline stands by FOREVER -- a deadlock, because the gate blocks the very
# STEP 2 ingest that would clear it (observed 2026-07-06: 30 papers stuck all
# day, every cycle a 1s no-op). So on a closed gate, drain the backlog in-cycle
# ONCE (the same parallel-ingest STEP 2 runs anyway), then re-check. Only stand
# by if it is STILL closed afterwards. This adds no new work when gated (it only
# ingests the existing backlog, unlike harvesting, which would grow it) and the
# single-instance lock we hold prevents any overlap during the drain.
if (-not $gateOpen -and -not $Force) {
  $pi0 = Join-Path $ingest "parallel-ingest.ps1"
  if (Test-Path $pi0) {
    Log "Gate CLOSED: draining wiki backlog in-cycle before standing by (self-heal against a dead wiki runner-loop)."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $pi0 -Workers $IngestWorkers -PerWorker $IngestPerWorker -MaxMinutes 120 2>&1 | ForEach-Object { Log "  [wiki] $_" }
    Log "  in-cycle drain exit=$LASTEXITCODE"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scripts "check_gate.ps1") -WikiProject $wiki 2>&1 | Tee-Object -Variable gateOut | Out-Null
    $gateOpen = ($LASTEXITCODE -eq 0)
    Log "STEP 0 gate re-check after drain: $gateOut (open=$gateOpen)"
  } else {
    Log "Gate CLOSED and cannot self-heal: parallel-ingest.ps1 not found at $pi0."
  }
}

if (-not $gateOpen -and -not $Force) {
  Log "Gate CLOSED after drain attempt: wiki backlog still not clear. Standing by; the 20-min wiki runner-loop retains priority. Exiting 0."
  Remove-Item $lockFile -Force -EA SilentlyContinue
  exit 0
}
if (-not $gateOpen -and $Force) { Log "Gate CLOSED but -Force set: proceeding anyway (test mode)." }

# ---- 1. FETCH --------------------------------------------------------
Log "STEP 1: harvest 30 last-month + 3 last-6h papers (AI/DS/ML/DL)"
& $py (Join-Path $scripts "fetch_papers.py") --config $cfg --top-k 30 --top-recent 3 --recent-hours 6 2>&1 | ForEach-Object { Log "  [fetch] $_" }
Log "  fetch exit=$LASTEXITCODE"

# ---- 2. INGEST new papers into the LLM-wiki --------------------------
Log "STEP 2: ingest new papers into LLM-wiki (parallel-ingest $IngestWorkers x $IngestPerWorker)"
$pi = Join-Path $ingest "parallel-ingest.ps1"
if (Test-Path $pi) {
  & powershell -NoProfile -ExecutionPolicy Bypass -File $pi -Workers $IngestWorkers -PerWorker $IngestPerWorker -MaxMinutes 120 2>&1 | ForEach-Object { Log "  [wiki] $_" }
  Log "  ingest exit=$LASTEXITCODE"
} else { Log "  [wiki] SKIP: parallel-ingest.ps1 not found at $pi" }

# ---- 3. REPRODUCE ----------------------------------------------------
Log "STEP 3: reproduce papers (budget ${ReproduceBudgetMin}m, oldest-first backfill; GPU/huge-data papers scaled down faithfully with deviations recorded)"
& $py (Join-Path $scripts "reproduce.py") --config $cfg --backfill --deadline-minutes $ReproduceBudgetMin 2>&1 | ForEach-Object { Log "  [repro] $_" }
Log "  reproduce exit=$LASTEXITCODE"

# NOTE ON ORDERING: the user-facing deliverables (webapp -> QA -> email) run
# immediately after reproduce, BEFORE the open-ended manim backfill and the git
# commit. Reproduce (bounded to ${ReproduceBudgetMin}m) plus an unbounded manim
# render used to consume the whole cycle window, so when the session/job was
# killed mid-run the webapp was never rebuilt and no email ever sent. reproduce
# already emits per-paper manim inline, so the STEP 7 backfill is only a
# gap-filler and is safe to defer to the tail.

# ---- 4. WEBAPP -------------------------------------------------------
Log "STEP 4: rebuild the static reproduction web app"
& $py (Join-Path $scripts "build_webapp.py") --repo $repo 2>&1 | ForEach-Object { Log "  [web] $_" }
Log "  build_webapp exit=$LASTEXITCODE"

# ---- 5. QA loop ------------------------------------------------------
Log "STEP 5: QA agent fix loop"
$qa = Join-Path $scripts "qa_agent.py"
$qaEnabled = $true
try { if ($conf.qa -and ($conf.qa.enabled -eq $false)) { $qaEnabled = $false } } catch {}
if (-not $qaEnabled) {
  Log "  [qa] SKIP: qa disabled in config."
} elseif (Test-Path $qa) {
  & $py $qa --config $cfg --max-iters $QaMaxIterations 2>&1 | ForEach-Object { Log "  [qa] $_" }
  Log "  qa_agent exit=$LASTEXITCODE"
} else {
  Log "  [qa] SKIP: qa_agent.py not present yet at $qa (component not implemented); continuing."
}

# ---- 6. REPORT (unified PDF email) -----------------------------------
#  Sent BEFORE the open-ended manim backfill so the run reliably delivers even
#  if the cycle is killed during the (slow) animation render below.
Log "STEP 6: send unified PDF report"
& $py (Join-Path $scripts "send_report.py") --config $cfg 2>&1 | ForEach-Object { Log "  [mail] $_" }
Log "  send_report exit=$LASTEXITCODE"

# ---- 7. MANIM (safety net; reproduce already emits per-paper manim) --
#  Deferred to the tail: this is an open-ended render and the least critical of
#  the deliverables. Anything it produces ships in the NEXT cycle's webapp/email.
Log "STEP 7: make_manim backfill (render any missing per-paper animations)"
$mm = Join-Path $scripts "make_manim.py"
if (Test-Path $mm) {
  & $py $mm --backfill --timeout $ManimTimeout 2>&1 | ForEach-Object { Log "  [manim] $_" }
  Log "  make_manim exit=$LASTEXITCODE"
} else { Log "  [manim] SKIP: make_manim.py not found" }

# ---- 8. GITHUB SYNC (commit_and_push) --------------------------------
#  Runs LAST so it commits every artifact produced above (webapp, summaries,
#  freshly backfilled animations). github_sync.py reads its PAT from token_file
#  itself; the token is never passed through this script, never printed/committed.
Log "STEP 8: github sync (commit + push)"
$tokenFile = $null
try { $tokenFile = $conf.github.token_file } catch {}
$enablePush = $false
try { $enablePush = [bool]$conf.github.enable_push } catch {}
if (-not $enablePush) {
  Log "  [git] SKIP: github.enable_push=false in config. (Set it true once the private repo + token_file are ready.)"
} elseif ($tokenFile -and -not (Test-Path $tokenFile)) {
  Log "  [git] SKIP: token_file not present; cannot authenticate. (Path is configured; file is intentionally untracked.)"
} else {
  $msg = "session pipeline ${stamp}: harvest + ingest + reproduce + webapp + qa + email + manim"
  & $py (Join-Path $scripts "github_sync.py") $msg 2>&1 | ForEach-Object { Log "  [git] $_" }
  $syncExit = $LASTEXITCODE
  Log "  github_sync exit=$syncExit"

  # ---- 9. PUBLIC SYNC ------------------------------------------------
  #  Mirror ONLY the publishable subset (harness code + dashboard shell,
  #  never the AI/DS/ML/DL paper trees) to the PUBLIC repo
  #  agentic-research-advisor. Allowlist-first with a hard leak guard --
  #  see public_sync.py. Runs only after a successful private push. Active
  #  by default; disable by setting "public_sync": { "enabled": false } in
  #  config.json.
  $pubSync = $true
  if ($conf.PSObject.Properties['public_sync'] -and
      $conf.public_sync.PSObject.Properties['enabled']) {
    $pubSync = [bool]$conf.public_sync.enabled
  }
  if (-not $pubSync) {
    Log "  [public] SKIP: public_sync.enabled=false in config."
  } elseif ($syncExit -ne 0) {
    Log "  [public] SKIP: private github_sync did not succeed (exit=$syncExit)."
  } else {
    Log "STEP 9: public sync (mirror publishable subset -> agentic-research-advisor)"
    & $py (Join-Path $scripts "public_sync.py") 2>&1 | ForEach-Object { Log "  [public] $_" }
    Log "  public_sync exit=$LASTEXITCODE"
  }
}

Log "SESSION PIPELINE COMPLETE."
Remove-Item $lockFile -Force -EA SilentlyContinue
exit 0
