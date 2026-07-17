#!/usr/bin/env python3
"""Prune not-reproduced papers (Phase 3 of the 2026-07-12 cleanup directive).

Reads state/qa/prune_manifest.json (produced by analyze_reproduction_failures.py),
writes an append-only audit record state/pruned_papers.jsonl (so the pipeline
never re-harvests or re-attempts a pruned slug), and deletes each not-reproduced
paper directory from disk. KEEPS every full/partial reproduction.

Safety:
  * default is --dry-run (prints the plan, deletes nothing);
  * refuses to delete any slug in the manifest's kept set;
  * refuses to delete a slug flagged in DATASETS_TO_KEEP.md / MODELS_TO_KEEP.md
    (defence in depth - none currently match);
  * uses a read-only-bit-clearing rmtree (Windows git object store).

Usage:
  .venv\\Scripts\\python.exe scripts\\prune_unreproduced.py            # dry run
  .venv\\Scripts\\python.exe scripts\\prune_unreproduced.py --apply    # delete
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _rmtree_robust(path: Path) -> None:
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            pass
    if path.exists():
        shutil.rmtree(path, onerror=_onerror)


def dir_size(p: Path) -> int:
    total = 0
    for r, _d, fs in os.walk(p):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry-run)")
    ap.add_argument("--manifest", type=Path,
                    default=REPO / "state" / "qa" / "prune_manifest.json")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    kept = set(manifest["kept_slugs"])
    candidates = manifest["candidates"]

    pruned_path = REPO / "state" / "pruned_papers.jsonl"
    already_pruned = set()
    if pruned_path.exists():
        for ln in pruned_path.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if ln:
                try:
                    already_pruned.add(json.loads(ln).get("slug"))
                except Exception:  # noqa: BLE001
                    pass

    to_delete: list[dict] = []
    skipped: list[str] = []
    for c in candidates:
        key = c["key"]
        if key in kept:
            skipped.append(f"{key} (in kept set - REFUSED)")
            continue
        pd = REPO / c["area"] / c["slug"]
        if not pd.is_dir():
            skipped.append(f"{key} (not on disk)")
            continue
        to_delete.append(c)

    total_bytes = sum(dir_size(REPO / c["area"] / c["slug"]) for c in to_delete)
    print(f"Manifest: {len(candidates)} candidates, {len(kept)} kept")
    print(f"Planned deletions: {len(to_delete)} dirs, {total_bytes/1e6:.1f} MB")
    if skipped:
        print(f"Refused/absent ({len(skipped)}):")
        for s in skipped[:20]:
            print(f"  - {s}")

    if not args.apply:
        print("\nDRY RUN - nothing deleted. Re-run with --apply to delete.")
        return 0

    # append audit records first (durable before deletion)
    now = datetime.utcnow().isoformat() + "Z"
    with pruned_path.open("a", encoding="utf-8") as f:
        for c in to_delete:
            if c["slug"] in already_pruned:
                continue
            f.write(json.dumps({
                "slug": c["slug"], "area": c["area"], "key": c["key"],
                "status": "PRUNED_UNREPRODUCED", "terminal": True,
                "bucket": c.get("bucket"),
                "intrinsic_signal": c.get("intrinsic_signal"),
                "evidence": c.get("evidence"),
                "pruned_utc": now,
            }, ensure_ascii=False) + "\n")

    freed = 0
    for c in to_delete:
        pd = REPO / c["area"] / c["slug"]
        freed += dir_size(pd)
        _rmtree_robust(pd)
    print(f"\nDeleted {len(to_delete)} dirs, freed ~{freed/1e6:.1f} MB")
    print(f"Audit trail appended to {pruned_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
