# =====================================================================
#  Gating condition check.
#  The daily research pipeline must only take over once the hourly wiki
#  ingest has covered ALL new/unclassified files. This returns whether the
#  wiki backlog is fully drained.
#
#  Exit code 0 = gate OPEN (backlog clear). Exit code 1 = still gated.
#  Also prints a JSON line with the counts.
# =====================================================================
param(
  [string]$WikiProject = (Join-Path (Split-Path -Parent $PSScriptRoot) "AI_DS_ML_DL")
)
$ErrorActionPreference = 'Stop'
$enc = New-Object System.Text.UTF8Encoding($false)
$ing = Join-Path $WikiProject "wiki\.ingest"
$manifest = Join-Path $ing "manifest.jsonl"
$research = Join-Path $WikiProject "raw\Research"
$topics = 'AI','Data_Science','Deep_Learning','Machine_Learning'

$status = @{}
if (Test-Path $manifest) {
  foreach ($ln in [System.IO.File]::ReadLines($manifest, $enc)) {
    if ($ln.Trim()) { try { $o = $ln | ConvertFrom-Json; $status[$o.key] = $o.status } catch {} }
  }
}
$seen = @{}
foreach ($t in $topics) {
  $d = Join-Path $research "$t\markdown"
  if (Test-Path $d) { Get-ChildItem $d -File -Filter *.md | ForEach-Object { $seen[$_.Name] = 1 } }
}
$newUnclassified = (@($seen.Keys | Where-Object { -not $status.ContainsKey($_) })).Count
$pendingInScope  = (@($status.Keys | Where-Object { $status[$_] -eq 'pending' -and $seen.ContainsKey($_) })).Count
$clear = ($newUnclassified -eq 0 -and $pendingInScope -eq 0)

$result = [ordered]@{
  ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
  on_disk = $seen.Count
  new_unclassified = $newUnclassified
  pending_in_scope = $pendingInScope
  gate_open = $clear
}
$result | ConvertTo-Json -Compress
if ($clear) { exit 0 } else { exit 1 }
