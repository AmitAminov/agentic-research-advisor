# =====================================================================
#  Commit everything in the researcher repo and (if configured) push.
#  Safe to run daily. Reads github settings from config.json.
#  Push only happens when github.enable_push=true and a remote is set.
# =====================================================================
param(
  [string]$Repo = (Split-Path -Parent $PSScriptRoot),
  [string]$Message = $null
)
$ErrorActionPreference = 'Stop'
Set-Location $Repo

$cfgPath = Join-Path $Repo "config.json"
$cfg = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw | ConvertFrom-Json } else { $null }
if (-not $Message) { $Message = "daily: corpus + reproductions + webapp ($(Get-Date -Format yyyy-MM-dd))" }

git add -A 2>&1 | Out-Null
$pending = git status --porcelain
if (-not $pending) { Write-Output "[git] nothing to commit."; }
else {
  # commit identity: config.json (github.git_user_name/email) if set, else git's own config
  $idArgs = @()
  if ($cfg -and $cfg.github.git_user_name)  { $idArgs += @("-c", "user.name=$($cfg.github.git_user_name)") }
  if ($cfg -and $cfg.github.git_user_email) { $idArgs += @("-c", "user.email=$($cfg.github.git_user_email)") }
  git @idArgs commit -q -m $Message 2>&1 | Out-Null
  Write-Output "[git] committed: $Message"
}

if ($cfg -and $cfg.github.enable_push -and $cfg.github.remote_url) {
  $branch = if ($cfg.github.branch) { $cfg.github.branch } else { "main" }
  $hasRemote = (git remote) -contains "origin"
  if (-not $hasRemote) { git remote add origin $cfg.github.remote_url; Write-Output "[git] added origin $($cfg.github.remote_url)" }
  else { git remote set-url origin $cfg.github.remote_url }
  try {
    git push -u origin $branch 2>&1 | Out-Null
    Write-Output "[git] pushed to origin/$branch"
  } catch {
    Write-Output "[git] push failed (check credentials / remote): $_"
  }
} else {
  Write-Output "[git] push disabled or no remote configured (see SETUP.md). Commits are local only."
}
