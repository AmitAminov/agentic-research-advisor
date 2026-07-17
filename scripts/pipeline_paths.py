#!/usr/bin/env python3
"""Shared path/executable resolution for the pipeline scripts.

Every location the harness needs is resolved with the same precedence:

    1. an explicit, non-empty value in config.json
    2. an environment variable override
    3. a portable default relative to this repository (or the running
       interpreter / PATH for executables)

so a fresh clone works without editing any absolute paths, while an operator
can still pin everything down in config.json for unattended scheduled runs.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

# scripts/ lives directly under the repo root.
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg_get(cfg: dict[str, Any] | None, section: str, key: str) -> str | None:
    """Return a non-empty string value from config, else None."""
    if not cfg:
        return None
    val = (cfg.get(section) or {}).get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def repo_root(cfg: dict[str, Any] | None = None) -> Path:
    """The researcher repo root (holds AI/ DS/ ML/ DL/ state/ webapp/ ...)."""
    explicit = _cfg_get(cfg, "paths", "repo_root") or os.environ.get("RESEARCHER_REPO_ROOT")
    return Path(explicit).resolve() if explicit else DEFAULT_REPO_ROOT


def python_exe(cfg: dict[str, Any] | None = None) -> str:
    """Interpreter handed to subprocesses (defaults to the one running us)."""
    return (_cfg_get(cfg, "paths", "python")
            or os.environ.get("RESEARCHER_PYTHON")
            or sys.executable)


def claude_exe(cfg: dict[str, Any] | None = None) -> str:
    """The claude CLI used for headless worker invocations."""
    return (_cfg_get(cfg, "reproduce", "claude_exe")
            or os.environ.get("CLAUDE_EXE")
            or shutil.which("claude")
            or "claude")


def wiki_project(cfg: dict[str, Any] | None = None) -> Path:
    """The companion LLM-wiki project directory (optional component)."""
    explicit = _cfg_get(cfg, "paths", "wiki_project") or os.environ.get("RESEARCHER_WIKI_PROJECT")
    return Path(explicit).resolve() if explicit else repo_root(cfg) / "AI_DS_ML_DL"


def token_file(cfg: dict[str, Any] | None = None) -> Path | None:
    """Optional GitHub PAT file (github.token_file / GITHUB_TOKEN_FILE).

    Returns None when unconfigured; callers must treat that as "no token".
    The token itself must never be printed, logged, or written anywhere.
    """
    explicit = _cfg_get(cfg, "github", "token_file") or os.environ.get("GITHUB_TOKEN_FILE")
    return Path(explicit) if explicit else None


def resolve_under_repo(value: str | Path, cfg: dict[str, Any] | None = None) -> Path:
    """Resolve a possibly-relative configured path against the repo root."""
    p = Path(value)
    return p if p.is_absolute() else repo_root(cfg) / p


def ensure_corpus_dedup_state(raw_research_dir: Path, cfg: dict[str, Any] | None = None) -> bool:
    """Hydrate the harvest dedup state from the cloud corpus on a local miss.

    The paper corpus is offloaded to ``gdrive:AI_DS_ML_DL_Researcher/corpus`` (rclone
    remote from ``RCLONE_REMOTE``, default ``gdrive``). Harvest only needs each area's
    ``analyzed_articles.pkl`` to skip already-known titles — NOT the 10+ GB of PDFs —
    so this pulls ONLY those small dedup files, preserving dedup continuity across
    runs without re-filling local disk.

    Best-effort: returns ``False`` (leaving the directory untouched) when rclone or
    the remote is unavailable, so harvest still runs — it just cannot dedup against
    history that round. Never raises.
    """
    import subprocess

    rclone = os.environ.get("RCLONE_BIN") or shutil.which("rclone")
    if not rclone:
        return False
    remote = os.environ.get("RCLONE_REMOTE", "gdrive")
    src = f"{remote}:AI_DS_ML_DL_Researcher/corpus"
    try:
        raw_research_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [rclone, "copy", src, str(raw_research_dir),
             "--include", "**/analyzed_articles.pkl",
             "--checksum", "--transfers", "4", "--tpslimit", "10"],
            check=True,
        )
    except Exception:  # noqa: BLE001 — best-effort hydration; harvest degrades gracefully
        return False
    return True
