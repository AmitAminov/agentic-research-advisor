"""
asset_registry.py — on-demand resolver for cloud-hosted models, wikis, and docs.

Both AI_DS_ML_DL_Researcher and UnifiedML (and the Claude/Codex agents) call
resolve(kind, id) to obtain a LOCAL path for an asset that lives in Google Drive
(rclone remote `gdrive:`). On a cache miss it pulls from Drive, verifies integrity
with `rclone check --checksum`, caches locally, and returns the path. Nothing is
streamed off the mount during training — assets are staged to a local cache first.

Drive layout (see MIGRATION_PLAN_AIDS_UNIFIEDML.md):
    gdrive:ML_MODELS/...      kind="model"   (e.g. "nas-bench-201/NAS-Bench-201-v1_1-096897.pth")
    gdrive:KNOWLEDGE/wiki     kind="wiki"
    gdrive:KNOWLEDGE/...      kind="doc"

Catalogs:
    gdrive:ML_MODELS/models_manifest.csv
    gdrive:KNOWLEDGE/knowledge_manifest.csv

Env:
    ML_CACHE       local cache root (default: %LOCALAPPDATA%\\ml-cache or ~/.cache/ml-cache)
    RCLONE_REMOTE  rclone remote name (default: "gdrive")
    RCLONE_BIN     path to rclone (default: "rclone" on PATH)

This module shells out to rclone; it has no third-party dependencies.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")
_RCLONE = os.environ.get("RCLONE_BIN", "rclone")

# Drive top-level folder per asset kind.
_KIND_ROOT = {
    "model": "ML_MODELS",
    "wiki": "KNOWLEDGE/wiki",
    "doc": "KNOWLEDGE",
}


def _cache_root() -> Path:
    env = os.environ.get("ML_CACHE")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "ml-cache"


def _run(args: list[str]) -> None:
    subprocess.run(args, check=True)


def resolve(kind: str, asset_id: str) -> Path:
    """Return a local path for `asset_id` of the given `kind`, pulling from Drive on cache miss.

    kind: "model" | "wiki" | "doc"
    asset_id: path relative to the kind's Drive root, e.g.
        resolve("model", "nas-bench-201/NAS-Bench-201-v1_1-096897.pth")
        resolve("wiki",  "")                       # the whole wiki tree
        resolve("doc",   "documents_papers")
    """
    if kind not in _KIND_ROOT:
        raise ValueError(f"unknown asset kind {kind!r}; expected one of {sorted(_KIND_ROOT)}")

    remote_path = f"{_REMOTE}:{_KIND_ROOT[kind]}"
    if asset_id:
        remote_path = f"{remote_path}/{asset_id}"

    local = _cache_root() / kind / (asset_id or "_root")
    if local.exists():
        return local  # cache hit

    if shutil.which(_RCLONE) is None and not os.path.isfile(_RCLONE):
        raise RuntimeError(
            f"rclone not found ({_RCLONE!r}); configure the `{_REMOTE}:` remote "
            "and set RCLONE_BIN if it is not on PATH."
        )

    local.parent.mkdir(parents=True, exist_ok=True)
    dest = str(local)
    common = ["--checksum", "--transfers", "4", "--tpslimit", "10"]

    # A single-file asset (has a suffix) copies into its parent dir; a tree copies into `dest`.
    if asset_id and Path(asset_id).suffix:
        _run([_RCLONE, "copy", remote_path, str(local.parent), *common])
        _run([_RCLONE, "check", remote_path, str(local.parent), "--checksum", "--one-way"])
    else:
        _run([_RCLONE, "copy", remote_path, dest, *common])
        _run([_RCLONE, "check", remote_path, dest, "--checksum", "--one-way"])
    return local


def publish(kind: str, local_path: str, asset_id: str) -> str:
    """Push a new/updated asset back to Drive and return its Drive path.

    After training, call e.g. publish("model", run_dir/"best.pt", "myexp/best.pt").
    Verifies the upload with `rclone check --one-way` before returning.
    """
    if kind not in _KIND_ROOT:
        raise ValueError(f"unknown asset kind {kind!r}")
    remote_path = f"{_REMOTE}:{_KIND_ROOT[kind]}/{asset_id}"
    src = Path(local_path)
    remote_dir = remote_path.rsplit("/", 1)[0] if src.is_file() else remote_path
    _run([_RCLONE, "copy", str(src), remote_dir, "--checksum", "--tpslimit", "10"])
    _run([_RCLONE, "check", str(src), remote_dir, "--checksum", "--one-way"])
    return remote_path


# Convenience wrappers mirroring the plan's sketch.
def model(asset_id: str) -> Path:
    return resolve("model", asset_id)


def wiki(asset_id: str = "") -> Path:
    return resolve("wiki", asset_id)


def doc(asset_id: str) -> Path:
    return resolve("doc", asset_id)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Resolve a cloud asset to a local path.")
    ap.add_argument("kind", choices=sorted(_KIND_ROOT))
    ap.add_argument("id", nargs="?", default="")
    ns = ap.parse_args()
    print(resolve(ns.kind, ns.id))
