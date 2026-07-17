# =====================================================================
#  HEAVY-DATASET CLEANUP  (daily reproduction-agent routine)
#
#  Permanently deletes DOWNLOADED datasets that are:
#    * larger than -MinSizeMB (default 50 MB),
#    * older than -OlderThanDays (default 7) by LastWriteTime,
#    * a downloaded-data file type (archives / weights / tensors / tables /
#      csv-jsonl data — see $DataExt), and
#    * NOT protected (see below).
#  inside AI_DS_ML_DL_Researcher\ and UnifiedML\.
#
#  NEVER deleted (protection layers, all enforced):
#    1. anything matching a Keep pattern in
#       C:\Users\ADMIN\Agentic_Projects\DATASETS_TO_KEEP.md (user- or agent-edited),
#    2. files with a co-located *.sha256 pin (pinned artifacts),
#    3. anything under .git\ .venv\ venv\ env\ site-packages\ node_modules\,
#    4. generated outputs: reproduced_results\ original_results\ manim\ _media\ .ffmpeg\ .ffbin\,
#    5. tools / non-data types (.exe/.dll/.so/... — only $DataExt types are candidates),
#    6. git-TRACKED files (only untracked/gitignored downloads are removable),
#    7. anything newer than the age cutoff (one-week grace for active work).
#
#  Every action is logged to logs\dataset-cleanup-<date>.log. Use -DryRun to
#  preview without deleting.
#
#  Run now:      powershell -File cleanup_heavy_datasets.ps1
#  Preview only: powershell -File cleanup_heavy_datasets.ps1 -DryRun
# =====================================================================
param(
  [switch]$DryRun,
  [int]$MinSizeMB = 50,
  [int]$OlderThanDays = 7
)
$ErrorActionPreference = 'Continue'

$Roots     = @('C:\Users\ADMIN\Agentic_Projects\AI_DS_ML_DL_Researcher',
               'C:\Users\ADMIN\Agentic_Projects\UnifiedML')
$KeepFiles = @('C:\Users\ADMIN\Agentic_Projects\DATASETS_TO_KEEP.md',
               'C:\Users\ADMIN\Agentic_Projects\MODELS_TO_KEEP.md')
$LogDir    = 'C:\Users\ADMIN\Agentic_Projects\AI_DS_ML_DL_Researcher\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("dataset-cleanup-{0}.log" -f (Get-Date -Format 'yyyy-MM-dd_HHmmss'))
function Log($m){ $l = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m; Add-Content $Log $l -Encoding utf8; Write-Output $l }

# Only these extensions are ever candidates (downloaded-data types).
$DataExt = @('.zip','.tar','.gz','.tgz','.bz2','.xz','.7z','.rar',
             '.h5','.hdf5','.npz','.npy','.parquet','.arrow','.feather',
             '.pth','.pt','.ckpt','.safetensors','.bin','.onnx','.gguf','.msgpack',
             '.pkl','.pickle','.joblib','.jsonl','.csv','.tsv',
             '.lmdb','.mdb','.rec','.ubyte','.pb','.tflite','.model','.vec','.npy')
# Path fragments that are always protected.
$ExclFrag = @('\.git\','\.venv\','\venv\','\env\','\site-packages\','\node_modules\',
              '\reproduced_results\','\original_results\','\manim\','\_media\','\.ffmpeg\','\.ffbin\')

# Parse Keep patterns from BOTH keep-lists. ONLY bullet lines under a "## Keep"
# header are patterns — this keeps prose (which mentions .git\, .venv\, etc.)
# from being misread as keep globs.
$KeepPatterns = @()
foreach ($kf in $KeepFiles) {
  if (-not (Test-Path $kf)) { continue }
  $inKeep = $false
  foreach ($line in Get-Content $kf) {
    if ($line -match '^\s*##\s') { $inKeep = ($line -match '^\s*##\s*Keep\b'); continue }
    if (-not $inKeep) { continue }
    if ($line -notmatch '^\s*[-*]\s') { continue }    # only bullet entries
    $t = ($line.Trim() -replace '^[-*]\s*','')        # drop bullet
    $t = ($t -split '#')[0].Trim()                    # drop trailing comment
    $t = ($t -split '<!--')[0].Trim()                 # drop html comment
    if ($t.Length -ge 3 -and ($t -match '[\\/]')) {
      if ($t -notmatch '^[A-Za-z]:') { $t = Join-Path 'C:\Users\ADMIN\Agentic_Projects' $t }
      $KeepPatterns += $t
    }
  }
}
function Test-Kept([string]$path){ foreach ($p in $KeepPatterns){ if ($path -like $p){ return $true } }; return $false }
function Test-Tracked([string]$repo,[string]$path){
  try { & git -C $repo ls-files --error-unmatch -- "$path" *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

$cutoff  = (Get-Date).AddDays(-$OlderThanDays)
$deleted = 0; $freed = [long]0; $wouldFree = [long]0; $kept = 0
Log ("CLEANUP START dryrun=$DryRun min=${MinSizeMB}MB older-than=${OlderThanDays}d keep-patterns=$($KeepPatterns.Count)")

foreach ($r in $Roots) {
  if (-not (Test-Path $r)) { Log "  (root missing, skipping: $r)"; continue }
  Get-ChildItem -LiteralPath $r -Recurse -File -EA SilentlyContinue |
    Where-Object { $_.Length -gt ($MinSizeMB * 1MB) -and $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
      $f = $_; $fn = $f.FullName
      if ($DataExt -notcontains $f.Extension.ToLower()) { return }              # not a data type
      foreach ($frag in $ExclFrag) { if ($fn -like "*$frag*") { return } }      # protected dir
      if ((Test-Path -LiteralPath ($fn + '.sha256')) -or
          (Test-Path -LiteralPath (Join-Path $f.DirectoryName ($f.BaseName + '.sha256')))) {
        Log "  KEEP sha256-pinned : $fn"; $script:kept++; return }
      if (Test-Kept $fn) { Log "  KEEP keep-list     : $fn"; $script:kept++; return }
      if (Test-Tracked $r $fn) { Log "  KEEP git-tracked   : $fn"; $script:kept++; return }
      $mb  = [math]::Round($f.Length / 1MB)
      $age = [math]::Round(((Get-Date) - $f.LastWriteTime).TotalDays, 1)
      if ($DryRun) { Log ("  WOULD-DELETE {0}MB age={1}d : {2}" -f $mb,$age,$fn); $script:wouldFree += $f.Length }
      else {
        try { Remove-Item -LiteralPath $fn -Force -EA Stop
              Log ("  DELETED {0}MB age={1}d : {2}" -f $mb,$age,$fn); $script:deleted++; $script:freed += $f.Length }
        catch { Log "  DELETE-FAILED : $fn : $_" }
      }
    }
}
if ($DryRun) { Log ("CLEANUP DRY-RUN DONE: would delete files freeing {0:N2} GB (kept {1} protected)" -f ($wouldFree/1GB), $kept) }
else         { Log ("CLEANUP DONE: deleted {0} file(s), freed {1:N2} GB (kept {2} protected)" -f $deleted, ($freed/1GB), $kept) }
