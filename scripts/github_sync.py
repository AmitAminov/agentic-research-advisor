"""
github_sync.py -- manage the PRIVATE GitHub repo for the AI/DS/ML/DL researcher.

Responsibilities
----------------
1. Read a GitHub Personal Access Token (PAT) from a local, untracked file.
   The token is NEVER printed, logged, or written to any tracked file.
2. Talk to the GitHub REST API (via the shared ``polite_http`` client --
   mailto UA, throttled, Retry-After/x-ratelimit-aware; NETWORK_ETIQUETTE.md) to:
     * identify the authenticated user      -> GET  /user
     * create a PRIVATE repo (idempotent)    -> POST /user/repos {private: true}
3. Configure the local git remote ``origin`` to a CLEAN https URL (no token)
   and push over HTTPS by supplying an authenticated URL *in memory only*
   (as a subprocess argument), so the token never lands in .git/config or any
   tracked file.

Public functions
----------------
    ensure_repo()            -> dict with keys: owner, name, html_url, clone_url
    commit_and_push(message) -> bool (True when the push to origin/main succeeds)

CPU-only / no external services beyond the GitHub API are required.

Constraints honoured:
  * Windows, Python 3.10 (.venv interpreter).
  * Reads config.json but NEVER modifies it (only the orchestrator edits it).
  * Does not print or commit the GitHub token.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline_paths  # noqa: E402
import polite_http  # noqa: E402  (shared polite HTTP client, NETWORK_ETIQUETTE.md)
from secrets_helper import get_secret  # noqa: E402

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
REPO_ROOT = pipeline_paths.DEFAULT_REPO_ROOT
CONFIG_FILE = REPO_ROOT / "config.json"

REPO_NAME = "AI_DS_ML_DL_Researcher"
BRANCH = "main"

# The pipeline's OWN output paths -- the only things commit_and_push stages.
# Everything else (scripts/, docs/, README, config.json, tests/, CI) belongs to
# the maintainer and is never touched by an automated cycle. .gitignore still
# filters inside these (manim intermediates, bulk raw data). AI_DS_ML_DL holds
# the LLM-wiki the ingest step grows.
OUTPUT_PATHS = ("AI", "DS", "ML", "DL", "webapp", "state", "AI_DS_ML_DL")

GITHUB_API = "https://api.github.com"
# The User-Agent (mailto contact + "+github-sync" suffix) is set by polite_http,
# which also throttles, honors Retry-After / x-ratelimit-*, and raises
# ProviderBlocked on persistent 429/403/503 (NETWORK_ETIQUETTE.md).
API_HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_UA_SUFFIX = "+github-sync"


# --------------------------------------------------------------------------- #
# Token handling -- keep it out of logs at all costs
# --------------------------------------------------------------------------- #
def _read_token() -> str:
    """Return the PAT with surrounding whitespace stripped.

    Resolution order (changed 2026-07-17): **Google Secret Manager FIRST**
    (secret 'github-pat', ADC -> gcloud CLI), then the GITHUB_TOKEN env var,
    then the optional local token file. This host carries a stale GITHUB_TOKEN
    in the user AND machine environment that GitHub rejects with 403 on push;
    env-first resolution made every unattended push fail (the 2026-07-15/16
    session artifacts were left committed-but-unpushed by exactly this). The
    canonical credential is Secret Manager's github-pat (user-level policy);
    env is only a fallback when SM/gcloud are unavailable. The token is only
    ever held in a local variable; never printed, logged, or written anywhere.
    """
    token_file = pipeline_paths.token_file(_load_config())
    try:
        token = get_secret("github-pat", env=None).strip()  # SM/gcloud only
    except Exception:  # noqa: BLE001 - fall back to env/file below
        token = ""
    if not token:
        token = get_secret(
            "github-pat", env="GITHUB_TOKEN",
            file_fallback=str(token_file) if token_file else None,
        ).strip()
    if not token:
        raise ValueError("GitHub token is empty.")
    return token


def _auth_headers(token: str) -> dict:
    headers = dict(API_HEADERS_BASE)
    headers["Authorization"] = f"Bearer {token}"
    return headers


# --------------------------------------------------------------------------- #
# config.json (read-only)
# --------------------------------------------------------------------------- #
def _load_config() -> dict:
    """Read config.json if present. This function NEVER writes to it."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


# --------------------------------------------------------------------------- #
# Low-level git helpers
# --------------------------------------------------------------------------- #
def _git(*args: str, check: bool = True,
         capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command inside REPO_ROOT.

    NOTE: callers must never pass the token in *args unless the result is
    guaranteed not to be surfaced to logs. The single place that does so
    (the push) suppresses output explicitly.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        check=check,
        capture_output=capture,
        text=True,
    )


def _clean_https_url(owner: str) -> str:
    """Tokenless HTTPS URL that is safe to store in .git/config."""
    return f"https://github.com/{owner}/{REPO_NAME}.git"


def _authed_push_url(owner: str, token: str) -> str:
    """Authenticated URL used ONLY as an in-memory subprocess argument.

    Never stored in config and never returned to callers/loggers.
    """
    return f"https://x-access-token:{token}@github.com/{owner}/{REPO_NAME}.git"


# --------------------------------------------------------------------------- #
# GitHub REST API
# --------------------------------------------------------------------------- #
def _get_authenticated_user(token: str) -> str:
    """Return the authenticated user's login (GET /user)."""
    resp = polite_http.get(
        f"{GITHUB_API}/user",
        headers=_auth_headers(token),
        timeout=30,
        ua_suffix=_UA_SUFFIX,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GET /user failed: HTTP {resp.status_code} "
            f"({resp.json().get('message', 'unknown error')})"
        )
    return resp.json()["login"]


def _repo_exists(token: str, owner: str) -> Optional[dict]:
    """Return the repo JSON if it exists, else None (GET /repos/{owner}/{repo})."""
    resp = polite_http.get(
        f"{GITHUB_API}/repos/{owner}/{REPO_NAME}",
        headers=_auth_headers(token),
        timeout=30,
        ua_suffix=_UA_SUFFIX,
    )
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    raise RuntimeError(
        f"GET /repos/{owner}/{REPO_NAME} failed: HTTP {resp.status_code} "
        f"({resp.json().get('message', 'unknown error')})"
    )


def _create_private_repo(token: str) -> dict:
    """Create a PRIVATE repo (POST /user/repos)."""
    payload = {
        "name": REPO_NAME,
        "private": True,
        "description": "Autonomous AI/DS/ML/DL research reproduction pipeline.",
        "auto_init": False,
        "has_issues": True,
        "has_wiki": False,
        "has_projects": False,
    }
    resp = polite_http.post(
        f"{GITHUB_API}/user/repos",
        headers=_auth_headers(token),
        json=payload,
        timeout=30,
        ua_suffix=_UA_SUFFIX,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"POST /user/repos failed: HTTP {resp.status_code} "
            f"({resp.json().get('message', 'unknown error')})"
        )
    return resp.json()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def ensure_repo() -> dict:
    """Ensure the PRIVATE repo exists on GitHub and that local ``origin`` is
    wired to its clean (tokenless) HTTPS URL.

    Idempotent: safe to call repeatedly. Returns a dict:
        {owner, name, html_url, clone_url, private}
    """
    token = _read_token()
    owner = _get_authenticated_user(token)

    repo = _repo_exists(token, owner)
    created = False
    if repo is None:
        repo = _create_private_repo(token)
        created = True

    info = {
        "owner": owner,
        "name": repo["name"],
        "html_url": repo["html_url"],
        "clone_url": repo["clone_url"],
        "private": repo.get("private", True),
        "created": created,
    }

    # Wire local remote 'origin' to the tokenless HTTPS URL (safe to persist).
    clean_url = _clean_https_url(owner)
    remotes = _git("remote", check=False).stdout.split()
    if "origin" in remotes:
        _git("remote", "set-url", "origin", clean_url)
    else:
        _git("remote", "add", "origin", clean_url)

    # token goes out of scope here; never returned or logged.
    return info


def commit_and_push(message: str) -> bool:
    """Stage everything (respecting .gitignore), commit if there are changes,
    then push to origin/main over HTTPS using an in-memory authenticated URL.

    Returns True when the push succeeds (or when everything is already
    up to date), False on push failure.
    """
    token = _read_token()
    owner = _get_authenticated_user(token)

    # Make sure we are on the correct branch.
    _git("checkout", "-B", BRANCH, check=False)

    # Stage ONLY the pipeline's own output paths -- NEVER `git add -A`.
    # This keeps a concurrent human edit to scripts/, README, config.json,
    # .gitignore, tests/, etc. out of the automated commit: an unattended
    # cycle must never sweep the maintainer's uncommitted working changes.
    # .gitignore is still respected within each path (manim intermediates,
    # bulk raw datasets, secrets).
    for rel in OUTPUT_PATHS:
        if (REPO_ROOT / rel).exists():
            _git("add", "--", rel, check=False)

    # Commit only if the staging area actually has pipeline output changes.
    # (Unstaged human edits elsewhere in the tree must not trigger a commit.)
    staged = _git("diff", "--cached", "--name-only").stdout.strip()
    if staged:
        # Commit identity: config.json (github.git_user_name/email) if set,
        # otherwise whatever git itself is configured with.
        gh_cfg = _load_config().get("github", {})
        identity: list[str] = []
        if gh_cfg.get("git_user_name"):
            identity += ["-c", f"user.name={gh_cfg['git_user_name']}"]
        if gh_cfg.get("git_user_email"):
            identity += ["-c", f"user.email={gh_cfg['git_user_email']}"]
        _git(*identity, "commit", "-q", "-m", message)
        n = len(staged.splitlines())
        print(f"[git] committed {n} pipeline-output path(s): {message}")
    else:
        print("[git] nothing to commit (no pipeline-output changes).")

    # Push to the CLEAN origin URL, authenticating via an in-memory credential
    # helper (the user-level standard pattern, proven 2026-07-17: a push by
    # token-embedded URL was refused with 403 while the same token via the
    # credential helper succeeded). GIT_TERMINAL_PROMPT=0 fails fast instead of
    # popping a logon prompt; the token lives only in the child environment for
    # the duration of the command and is never stored, printed, or committed.
    clean_url = _clean_https_url(owner)
    push_env = dict(os.environ)
    push_env["GIT_TERMINAL_PROMPT"] = "0"
    push_env["GIT_PUSH_TOKEN"] = token
    helper = "!f() { echo username=x-access-token; echo \"password=$GIT_PUSH_TOKEN\"; }; f"
    proc = subprocess.run(
        ["git", "-c", "credential.helper=", "-c", f"credential.helper={helper}",
         "push", clean_url, f"HEAD:{BRANCH}"],
        cwd=str(REPO_ROOT),
        env=push_env,
        capture_output=True,
        text=True,
    )

    if proc.returncode == 0:
        # Sync the remote-tracking ref and bind upstream to the CLEAN origin
        # remote (never the authed URL). Best-effort; failures are non-fatal.
        _git("fetch", "origin", check=False)
        _git("branch", f"--set-upstream-to=origin/{BRANCH}", BRANCH, check=False)
        print(f"[git] pushed to origin/{BRANCH}.")
        return True

    # Scrub any accidental token echo before surfacing an error snippet.
    safe_err = (proc.stderr or "").replace(token, "***").strip()
    print(f"[git] push FAILED (rc={proc.returncode}). "
          f"See sanitized message below:")
    # Only show the last line to avoid noise; token already scrubbed.
    if safe_err:
        print("      " + safe_err.splitlines()[-1])
    return False


# --------------------------------------------------------------------------- #
# CLI entry point -- runs the initial ensure + commit + push.
# --------------------------------------------------------------------------- #
def main(argv: Optional[list] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    message = argv[0] if argv else "Initial commit: researcher pipeline scaffold"

    info = ensure_repo()
    state = "created" if info["created"] else "already exists"
    print(f"[github] repo {state}: {info['html_url']} "
          f"(private={info['private']})")

    ok = commit_and_push(message)
    print(f"[github] repo URL : {info['html_url']}")
    print(f"[github] push ok  : {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
