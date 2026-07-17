#!/usr/bin/env python
"""public_sync.py -- mirror the PUBLISHABLE subset of the private research repo
to the PUBLIC repo ``AmitAminov/agentic-research-advisor``.

Design (see also github_sync.py, which handles the PRIVATE repo only):
  * ALLOWLIST-FIRST. Only explicitly named paths are published; the default is
    exclusion. The per-paper reproduction trees (AI/ DS/ ML/ DL/, webapp/papers/),
    the LLM wiki, runtime state, logs, secrets, and config.json are NEVER copied.
  * The webapp is REGENERATED as a shell via ``build_webapp.py --shell-only``,
    so it ships the rebranded UI + aggregate counts but no paper cards/titles.
  * A HARD LEAK ASSERTION walks the public working tree and aborts the push if
    any forbidden path is present -- belt-and-suspenders over the allowlist.
  * A dedicated public working clone (NOT a git worktree of the private repo, so
    the private .git and its paper trees can never be cross-pushed).
  * The GitHub token (github-pat) is fetched in-memory via secrets_helper and is
    never printed, logged, or written to .git/config (push uses an in-memory
    authenticated URL; the persisted remote is tokenless).

Usage:
  python scripts/public_sync.py --dry-run   # copy + build + leak-check, no push
  python scripts/public_sync.py             # ...then commit + push to public main
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_helper import get_secret  # noqa: E402

PRIVATE_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_ROOT = PRIVATE_ROOT.parent / "agentic-research-advisor-public"
PUBLIC_REPO = "AmitAminov/agentic-research-advisor"
BRANCH = "main"

# Exact files copied verbatim private -> public.
# NOTE: README.md and SETUP.md are DELIBERATELY EXCLUDED — the public repo keeps
# a hand-curated README/SETUP (the "curated public snapshot" note + the
# what-I-built-vs-agents table) that must NOT be clobbered by the private prose.
# Branding renames to those two are made by hand on the public side.
ALLOW_FILES = [
    ".github/workflows/ci.yml", ".gitignore", "LICENSE",
    "config.example.json", "requirements.txt", "requirements-dev.txt", "ruff.toml",
    "docs/manim_3b1b_guide.md",
]
# Glob families copied private -> public (files only).
ALLOW_GLOBS = ["scripts/*.py", "scripts/*.ps1", "scripts/webapp_assets/*", "tests/**/*"]
# Never publish these even if a glob would match.
SKIP = {"scripts/secrets_helper.py"}
# Managed dirs are wiped then rebuilt from the allowlist so deletions/renames
# propagate. Root files are overwritten (not deleted). .git is never touched.
MANAGED_DIRS = ["scripts", "tests", "webapp", ".github", "docs"]
# PUBLIC-ONLY files preserved across the managed-dir wipe: these live only in
# the public repo (hand-curated there) and must never be deleted by a sync.
# pages.yml is the GitHub Pages deploy workflow for the public dashboard
# (discovered nearly-deleted on 2026-07-17 - the wipe of .github dropped it).
KEEP_PUBLIC = [".github/workflows/pages.yml"]
# HARD leak guard: none of these may exist in the public tree before push.
FORBIDDEN = ["AI", "DS", "ML", "DL", "AI_DS_ML_DL", "state", "logs",
             "webapp/papers", "config.json", "scripts/secrets_helper.py",
             "docs/SKILLS_ADOPTION.md"]


def _run(cmd, token=None, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        err = p.stderr or ""
        if token:
            err = err.replace(token, "***")
        raise RuntimeError(f"cmd failed ({p.returncode}): {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}\n"
                           f"{err.strip()[-600:]}")
    return p


def _push_with_retry(authed: str, token: str, attempts: int = 4) -> None:
    """Push HEAD -> public BRANCH, retrying transient 403 / secondary-rate-limit
    blocks with exponential backoff (GitHub throttles bursts of pushes to a repo).
    Never rotates identity/region to evade a block -- it only waits and retries."""
    import time
    last = ""
    for i in range(attempts):
        p = subprocess.run(["git", "-C", str(PUBLIC_ROOT), "push", authed, f"HEAD:{BRANCH}"],
                           capture_output=True, text=True)
        if p.returncode == 0:
            return
        last = (p.stderr or "").replace(token, "***").strip()
        if i < attempts - 1:
            wait = 30 * (2 ** i)
            print(f"[public_sync] push blocked (attempt {i + 1}/{attempts}); backing off {wait}s...")
            time.sleep(wait)
    raise SystemExit(f"[public_sync] push failed after {attempts} attempts: {last[-300:]}")


def _ensure_clone(token: str) -> None:
    authed = f"https://x-access-token:{token}@github.com/{PUBLIC_REPO}.git"
    clean = f"https://github.com/{PUBLIC_REPO}.git"
    if not (PUBLIC_ROOT / ".git").exists():
        PUBLIC_ROOT.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "clone", "--depth", "1", authed, str(PUBLIC_ROOT)], token=token)
    else:
        _run(["git", "-C", str(PUBLIC_ROOT), "fetch", "--depth", "1", authed, BRANCH], token=token)
        _run(["git", "-C", str(PUBLIC_ROOT), "reset", "--hard", "FETCH_HEAD"])
        _run(["git", "-C", str(PUBLIC_ROOT), "clean", "-fd"], check=False)
    # Persist only the tokenless URL.
    _run(["git", "-C", str(PUBLIC_ROOT), "remote", "set-url", "origin", clean], check=False)


def _resolve_allowlist() -> set[str]:
    paths: set[str] = set()
    for f in ALLOW_FILES:
        if (PRIVATE_ROOT / f).is_file():
            paths.add(f)
    for g in ALLOW_GLOBS:
        for p in PRIVATE_ROOT.glob(g):
            if p.is_file():
                rel = p.relative_to(PRIVATE_ROOT).as_posix()
                if rel not in SKIP:
                    paths.add(rel)
    return paths


def _leak_assert() -> None:
    hits = [f for f in FORBIDDEN if (PUBLIC_ROOT / f).exists()]
    if hits:
        raise SystemExit("[public_sync] LEAK GUARD TRIPPED -- forbidden path(s) in public tree: "
                         + ", ".join(hits) + ". Aborting; nothing pushed.")


def main(argv=None) -> int:
    import shutil
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv

    # Secret Manager FIRST (2026-07-17): this host carries a stale GITHUB_TOKEN
    # in the user/machine environment that GitHub 403s on push; env is only a
    # fallback when SM/gcloud are unavailable (see github_sync._read_token).
    try:
        token = get_secret("github-pat", env=None).strip()
    except Exception:  # noqa: BLE001
        token = ""
    if not token:
        token = get_secret("github-pat", env="GITHUB_TOKEN").strip()
    if not token:
        raise SystemExit("[public_sync] empty github token")

    _ensure_clone(token)

    # Wipe managed dirs, then re-materialize from the allowlist - preserving
    # the public-only KEEP_PUBLIC files (e.g. the Pages deploy workflow).
    kept: dict[str, bytes] = {}
    for rel in KEEP_PUBLIC:
        f = PUBLIC_ROOT / rel
        if f.is_file():
            kept[rel] = f.read_bytes()
    for d in MANAGED_DIRS:
        tgt = PUBLIC_ROOT / d
        if tgt.exists():
            shutil.rmtree(tgt)
    for rel, blob in kept.items():
        dst = PUBLIC_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
    allow = _resolve_allowlist()
    for rel in sorted(allow):
        src, dst = PRIVATE_ROOT / rel, PUBLIC_ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # Regenerate the public webapp SHELL (no paper content) with live counts.
    _run([sys.executable, str(PRIVATE_ROOT / "scripts" / "build_webapp.py"),
          "--repo", str(PRIVATE_ROOT), "--shell-only",
          "--web-out", str(PUBLIC_ROOT / "webapp")], token=token)

    # HARD leak assertion before anything is staged.
    _leak_assert()

    head = _run(["git", "-C", str(PRIVATE_ROOT), "rev-parse", "--short", "HEAD"]).stdout.strip()
    _run(["git", "-C", str(PUBLIC_ROOT), "add", "-A"])
    staged = _run(["git", "-C", str(PUBLIC_ROOT), "diff", "--cached", "--name-only"]).stdout.strip()
    if not staged:
        print("[public_sync] public repo already up to date.")
        return 0
    print(f"[public_sync] {len(staged.splitlines())} path(s) changed vs public HEAD:")
    print("\n".join("    " + s for s in staged.splitlines()[:60]))

    if dry:
        print(f"[public_sync] DRY RUN -- not committing/pushing. Public tree staged at {PUBLIC_ROOT}.")
        return 0

    _run(["git", "-C", str(PUBLIC_ROOT), "commit", "-q", "-m",
          f"chore(public): sync publishable snapshot @ {head}"])
    authed = f"https://x-access-token:{token}@github.com/{PUBLIC_REPO}.git"
    _push_with_retry(authed, token)
    print(f"[public_sync] pushed public snapshot @ {head}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
