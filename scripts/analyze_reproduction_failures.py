#!/usr/bin/env python3
"""Root-cause analysis of un-reproduced papers in the researcher corpus.

Read-only. Classifies every paper directory under <repo>/{AI,DS,ML,DL}/ into a
KEPT reproduction (verdict full/partial with real reproduced output) or a
NOT-REPRODUCED candidate, and for each candidate assigns a root-cause bucket
from the per-paper reproduce log signatures, skip files, and summary.md.

Buckets (Amit directive 2026-07-12):
  NO_CODE_REPO, HARDWARE, DATA_UNAVAILABLE, OBSOLETE_DEPS, TIMEOUT,
  HARNESS_ERROR, SECURITY, NOT_A_REPRODUCIBLE_PAPER, NOT_ATTEMPTED

Emits:
  docs/FAILURE_ANALYSIS.md      human report (histogram + per-paper table)
  state/qa/prune_manifest.json  machine-readable manifest driving Phase 3

Usage:  .venv\\Scripts\\python.exe scripts\\analyze_reproduction_failures.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
AREAS = ("AI", "DS", "ML", "DL")
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".mp4"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "_media", "upstream",
             "node_modules", ".ipynb_checkpoints"}


def load_snapshot() -> dict[str, dict[str, Any]]:
    p = REPO / "state" / "qa" / "status_snapshot.json"
    data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    out: dict[str, dict[str, Any]] = {}
    for rec in data.get("papers", []):
        # snapshot id = "<AREA>-<slug>"; strip the leading "<code>-"
        pid = rec.get("id", "")
        code = rec.get("code", "")
        slug = pid[len(code) + 1:] if pid.startswith(code + "-") else pid
        out[f"{code}/{slug}"] = rec
    return out


def load_skiplist() -> set[str]:
    p = REPO / "state" / "security_skiplist.json"
    if not p.exists():
        return set()
    data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    return {e.get("slug") for e in data.get("entries", []) if e.get("slug")}


def count_files(d: Path, exts: set[str] | None = None) -> int:
    if not d.is_dir():
        return 0
    n = 0
    for p in d.rglob("*"):
        if not p.is_file() or any(part in SKIP_DIRS for part in p.parts):
            continue
        if exts is None or p.suffix.lower() in exts:
            n += 1
    return n


def assess(pd: Path) -> dict[str, Any]:
    src = pd / "src"
    n_src = 0
    if src.is_dir():
        for p in src.rglob("*.py"):
            if not any(part in SKIP_DIRS for part in p.parts):
                n_src += 1
    repro = pd / "reproduced_results"
    repro_imgs = count_files(repro, IMG_EXTS)
    has_metrics = (repro / "metrics.json").exists() or (repro / "results.json").exists()
    n_metrics = 0
    if (repro / "metrics.json").exists():
        try:
            m = json.loads((repro / "metrics.json").read_text(encoding="utf-8", errors="replace"))
            mm = m.get("metrics")
            n_metrics = len(mm) if isinstance(mm, (list, dict)) else 0
        except Exception:
            pass
    tests = len(list((pd / "tests").glob("test_*.py"))) if (pd / "tests").is_dir() else 0
    return {
        "src_files": n_src, "reproduced_imgs": repro_imgs,
        "has_metrics": has_metrics, "n_metrics": n_metrics, "tests": tests,
        "security_skip": (repro / "SECURITY_SKIP.txt").exists(),
        "hardware_skip": (repro / "HARDWARE_SKIP.txt").exists(),
    }


def latest_log(slug: str, logs: list[Path]) -> Path | None:
    key = f"reproduce-{slug[:40]}-"
    cands = [p for p in logs if p.name.startswith(key)]
    if not cands:
        return None
    # names end with -YYYYMMDD_HHMMSS.log ; lexical sort == chronological
    return sorted(cands)[-1]


def log_signature(log: Path | None) -> tuple[str, str]:
    """Return (signature, evidence_snippet)."""
    if log is None:
        return ("no_log", "no reproduce log on disk")
    try:
        raw = log.read_bytes()
    except Exception:
        return ("no_log", "log unreadable")
    if len(raw) == 0:
        return ("empty", "0-byte log (claude produced no output / immediate crash)")
    txt = raw.decode("utf-8", errors="replace")
    head = txt[:400]
    tail = txt[-400:]
    low = txt.lower()
    if "session limit" in low or "hit your session" in low:
        return ("session_limit", head.strip().splitlines()[0][:160])
    if "reached your" in low and "limit" in low or "usage-credits" in low or "fable" in low and "limit" in low:
        return ("usage_limit", head.strip().splitlines()[0][:160])
    if "529 overloaded" in low or "api error: 529" in low:
        return ("api_overload", head.strip().splitlines()[0][:160])
    orphan_markers = ("waiting for", "in the background", "will notify me",
                      "re-invoked automatically", "monitor will notify",
                      "pipeline is progressing", "run is in progress",
                      "training run is in progress", "waiting for the pipeline")
    if any(mk in low for mk in orphan_markers) and len(raw) < 2000:
        return ("orphaned_bg", tail.strip().replace("\n", " ")[:160])
    return ("substantive", tail.strip().replace("\n", " ")[:160])


# keyword pre-screens for substantive-but-unreproduced papers (best effort)
SEC_RE = re.compile(r"\b(prompt injection|jailbreak|backdoor|malware|exploit|red[- ]team|adversarial attack)\b", re.I)
SURVEY_RE = re.compile(r"\b(a survey|survey of|systematic review|a review of|literature review|position paper|roadmap|taxonomy of)\b", re.I)
HW_RE = re.compile(r"\b(a100|h100|v100|tpu|64 gpus?|32 gpus?|8[x× ]a100|8[x× ]gpus?|pre[- ]?training|trained on \d+ ?gpus?|megatron|175b|70b|405b|distributed training)\b", re.I)


def classify(area_slug: str, pd: Path, a: dict[str, Any],
             log: Path | None, skiplist: set[str]) -> dict[str, Any]:
    slug = area_slug.split("/", 1)[1]
    # 1. SECURITY (highest priority)
    if slug in skiplist or a["security_skip"]:
        return {"bucket": "SECURITY",
                "evidence": "on security skiplist / SECURITY_SKIP.txt present"}
    # 2. explicit hardware skip file
    if a["hardware_skip"]:
        return {"bucket": "HARDWARE", "evidence": "HARDWARE_SKIP.txt (INELIGIBLE_HARDWARE) written"}

    sig, ev = log_signature(log)
    logname = log.name if log else "-"

    if sig == "no_log":
        return {"bucket": "NOT_ATTEMPTED",
                "evidence": "no reproduce log — harvested but reproduction never ran"}
    if sig in ("usage_limit", "session_limit", "api_overload", "empty"):
        sub = {"usage_limit": "model usage/credit limit",
               "session_limit": "claude session limit",
               "api_overload": "API 529 overloaded",
               "empty": "empty log / immediate crash"}[sig]
        return {"bucket": "HARNESS_ERROR",
                "evidence": f"{sub}: '{ev}' [{logname}]"}
    if sig == "orphaned_bg":
        return {"bucket": "HARNESS_ERROR",
                "evidence": f"orphaned background job — agent paused for a monitor that the "
                            f"single-shot harness cannot resume: '{ev}' [{logname}]"}

    # substantive log but no reproduced output -> inspect paper.md + summary.md
    paper_md = ""
    pm = pd / "paper.md"
    if pm.exists():
        paper_md = pm.read_text(encoding="utf-8", errors="replace")[:6000]
    summary = ""
    sm = pd / "summary.md"
    if sm.exists():
        summary = sm.read_text(encoding="utf-8", errors="replace")
    blob = f"{paper_md}\n{summary}"

    if SEC_RE.search(blob):
        return {"bucket": "SECURITY", "evidence": f"security keywords in paper/summary [{logname}]"}
    if HW_RE.search(paper_md):
        return {"bucket": "HARDWARE", "evidence": f"hardware-scale keywords in paper.md [{logname}]"}
    if SURVEY_RE.search(paper_md[:2500]):
        return {"bucket": "NOT_A_REPRODUCIBLE_PAPER",
                "evidence": f"survey/review/position signals in paper.md [{logname}]"}
    low = summary.lower()
    if "no official repo" in low or "no code" in low or "implement from" in low:
        return {"bucket": "NO_CODE_REPO",
                "evidence": f"summary states no official code located [{logname}]"}
    if "timeout" in (log_signature(log)[0]) or "deadline" in low:
        return {"bucket": "TIMEOUT", "evidence": f"ran out of wall-clock budget [{logname}]"}
    return {"bucket": "TIMEOUT",
            "evidence": f"substantive run, no reproduced output (likely wall-clock cutoff) [{logname}]"}


def main() -> int:
    snapshot = load_snapshot()
    skiplist = load_skiplist()
    logs = sorted((REPO / "logs").glob("reproduce-*.log"))

    snap_fp = {k: v for k, v in snapshot.items()
               if v.get("verdict") in ("full", "partial")}
    on_disk_keys = {f"{code}/{pd.name}" for code in AREAS
                    if (REPO / code).is_dir()
                    for pd in (REPO / code).iterdir() if pd.is_dir()}
    snap_fp_missing = [k for k in snap_fp if k not in on_disk_keys]
    print(f"Snapshot full/partial: {len(snap_fp)}; missing from disk: "
          f"{len(snap_fp_missing)} -> {snap_fp_missing}")

    kept: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    flags: list[str] = []

    for code in AREAS:
        adir = REPO / code
        if not adir.is_dir():
            continue
        for pd in sorted(adir.iterdir()):
            if not pd.is_dir() or pd.name.startswith("."):
                continue
            key = f"{code}/{pd.name}"
            a = assess(pd)
            snap = snapshot.get(key)
            snap_verdict = snap.get("verdict") if snap else None
            # on-disk reproduced proxy
            reproduced_now = a["has_metrics"] and (a["reproduced_imgs"] > 0 or a["src_files"] > 0)

            # KEEP decision: authoritative snapshot full/partial, OR reproduced-now
            keep = (snap_verdict in ("full", "partial")) or reproduced_now
            row = {"key": key, "area": code, "slug": pd.name,
                   "snapshot_verdict": snap_verdict, "reproduced_now": reproduced_now,
                   **a}
            if keep:
                kept.append(row)
                # flag conflicts: snapshot minimal but reproduced-now, or vice versa
                if snap_verdict == "minimal" and reproduced_now:
                    flags.append(f"KEEP-on-doubt {key}: snapshot=minimal but has metrics+output now")
                if snap is None:
                    flags.append(f"KEEP {key}: newer than snapshot, looks reproduced (verify)")
                continue
            # not reproduced -> classify
            if snap is None:
                flags.append(f"CANDIDATE {key}: newer than snapshot (not in it), classified fresh")
            log = latest_log(pd.name, logs)
            cls = classify(key, pd, a, log, skiplist)
            row.update(cls)
            candidates.append(row)

    # histogram
    hist: dict[str, int] = {}
    for c in candidates:
        hist[c["bucket"]] = hist.get(c["bucket"], 0) + 1

    # HARNESS_ERROR sub-cause breakdown (from evidence prefix)
    subhist: dict[str, int] = {}
    for c in candidates:
        if c["bucket"] != "HARNESS_ERROR":
            continue
        ev = c.get("evidence", "")
        sub = ev.split(":", 1)[0] if ":" in ev else ev[:40]
        subhist[sub] = subhist.get(sub, 0) + 1

    # data-integrity check: any candidate that actually has reproduced output?
    leaked = [c["key"] for c in candidates
              if c["src_files"] > 0 or c["reproduced_imgs"] > 0 or c["has_metrics"]]

    # secondary keyword cross-tab over paper.md (best-effort intrinsic signal)
    xtab: dict[str, int] = {"security": 0, "hardware_scale": 0, "survey": 0, "none": 0}
    for c in candidates:
        pm = REPO / c["area"] / c["slug"] / "paper.md"
        txt = pm.read_text(encoding="utf-8", errors="replace")[:6000] if pm.exists() else ""
        if SEC_RE.search(txt):
            sig = "security"
        elif HW_RE.search(txt):
            sig = "hardware_scale"
        elif SURVEY_RE.search(txt[:2500]):
            sig = "survey"
        else:
            sig = "none"
        c["intrinsic_signal"] = sig
        xtab[sig] += 1

    print(f"Total on-disk papers: {len(kept) + len(candidates)}")
    print(f"KEPT (full/partial or reproduced-now): {len(kept)}")
    print(f"NOT-REPRODUCED candidates: {len(candidates)}")
    print("\nBucket histogram:")
    for b, n in sorted(hist.items(), key=lambda kv: -kv[1]):
        print(f"  {b:28s} {n}")
    print("\nHARNESS_ERROR sub-causes:")
    for s, n in sorted(subhist.items(), key=lambda kv: -kv[1]):
        print(f"  {s:60s} {n}")
    print("\nSecondary intrinsic keyword signal over ALL candidates (paper.md):")
    for s, n in sorted(xtab.items(), key=lambda kv: -kv[1]):
        print(f"  {s:16s} {n}")
    print(f"\nDATA-INTEGRITY: candidates that actually have some reproduced output "
          f"(should be 0): {len(leaked)}")
    for k in leaked:
        print(f"  !! {k}")
    print(f"\nFlags ({len(flags)}):")
    for f in flags:
        print(f"  - {f}")

    # write manifest
    manifest = {
        "generated_utc": datetime.utcnow().isoformat() + "Z",
        "total_on_disk": len(kept) + len(candidates),
        "kept": len(kept),
        "not_reproduced": len(candidates),
        "histogram": hist,
        "harness_subcauses": subhist,
        "intrinsic_signal_crosstab": xtab,
        "snapshot_full_partial": len(snap_fp),
        "snapshot_fp_missing_from_disk": snap_fp_missing,
        "code_only_no_results": [c["key"] for c in candidates if c["src_files"] > 0],
        "kept_slugs": [k["key"] for k in kept],
        "candidates": candidates,
        "flags": flags,
    }
    out = REPO / "state" / "qa" / "prune_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    # write FAILURE_ANALYSIS.md
    docs = REPO / "docs"
    docs.mkdir(exist_ok=True)
    lines = [
        "# Reproduction failure analysis",
        "",
        f"_Generated {datetime.utcnow().isoformat()}Z by "
        "`scripts/analyze_reproduction_failures.py` (read-only)._",
        "",
        "Root-cause analysis of every paper directory under `{AI,DS,ML,DL}/` that is "
        "**not reproduced** (verdict `minimal` / no reproduced metrics+output). "
        "Full/partial reproductions are KEPT and excluded from this table.",
        "",
        "## Summary",
        "",
        f"- On-disk papers: **{len(kept) + len(candidates)}**",
        f"- Kept (full/partial or reproduced-now): **{len(kept)}** "
        f"(snapshot lists {len(snap_fp)} full/partial; "
        f"{len(snap_fp_missing)} already deleted from disk: "
        f"{', '.join(snap_fp_missing) or 'none'})",
        f"- Not reproduced (candidates for pruning): **{len(candidates)}**",
        "",
        "## Headline finding",
        "",
        "The overwhelming root cause is **not** intrinsic paper difficulty — it is the "
        "reproduction harness itself. The per-paper harness drives a headless `claude` "
        "CLI, and in the large majority of runs that CLI returned an account-level "
        "**usage / session limit** (`You've hit your session limit`, "
        "`You've reached your Fable 5 limit`, `usage-credits`) or an **API 529 Overloaded** "
        "or **empty output** — producing zero code and zero results. Worse, the harness "
        "recorded each dead run in the dedup ledger as processed, so the paper was "
        "permanently burned and never retried. These are retryable infrastructure "
        "failures, not evidence that the papers are irreproducible (Phase 2 fixes this).",
        "",
        "## Bucket histogram",
        "",
        "| Bucket | Count | Meaning |",
        "|---|--:|---|",
    ]
    meanings = {
        "HARNESS_ERROR": "the headless-claude harness/infra failed (usage/session limit, API 529, empty output, orphaned background job) — NOT an intrinsic paper property; retryable",
        "NOT_ATTEMPTED": "harvested but the reproduction step never ran (no log)",
        "TIMEOUT": "substantive run that ran out of the per-paper wall-clock budget",
        "HARDWARE": "faithful result needs GPU>6GB / multi-GPU / CUDA-scale training (HARDWARE_ENVELOPE.md)",
        "DATA_UNAVAILABLE": "required data gated/huge/missing with no small stand-in",
        "OBSOLETE_DEPS": "unbuildable environment (e.g. TF1.x)",
        "NO_CODE_REPO": "no upstream code found, nothing to run",
        "NOT_A_REPRODUCIBLE_PAPER": "survey/review/position paper with no empirical result",
        "SECURITY": "adversarial/security benchmark — SKIPPED_SECURITY_POLICY (not a failure)",
    }
    for b, n in sorted(hist.items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{b}` | {n} | {meanings.get(b, '')} |")
    lines += [
        "",
        "### HARNESS_ERROR sub-causes",
        "",
        "| Sub-cause | Count |",
        "|---|--:|",
    ]
    for s, n in sorted(subhist.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {s} | {n} |")
    lines += [
        "",
        "### Secondary intrinsic signal (keyword scan of paper.md, best-effort)",
        "",
        "Independent of what the (usually infra-killed) run actually did, this is a "
        "cheap keyword scan of each candidate's `paper.md`. It flags papers that would "
        "*also* likely be blocked on intrinsic grounds if the harness were fixed — "
        "useful for the Phase-2 pre-screen and for deciding which pruned slugs deserve a "
        "future retry. Not a confirmed run outcome.",
        "",
        "| Signal | Count | Interpretation |",
        "|---|--:|---|",
        f"| `security` | {xtab['security']} | adversarial/security keywords — likely "
        "SKIPPED_SECURITY_POLICY on any retry |",
        f"| `hardware_scale` | {xtab['hardware_scale']} | A100/TPU/multi-GPU/pretraining "
        "keywords — likely INELIGIBLE_HARDWARE on any retry |",
        f"| `survey` | {xtab['survey']} | survey/review/position — likely "
        "NOT_A_REPRODUCIBLE_PAPER |",
        f"| `none` | {xtab['none']} | no intrinsic blocker detected — genuinely worth a "
        "retry once the harness is fixed |",
        "",
        "### Candidates that wrote code but produced NO results (killed mid-run)",
        "",
        "These have `src/` code but zero reproduced figures and no `metrics.json`, so "
        "they are not reproductions by the KEEP criterion. Deletion is git-recoverable, "
        "so the partial code is retained in history; they are the highest-value retry "
        "targets.",
        "",
    ]
    code_only = sorted([c for c in candidates if c["src_files"] > 0],
                       key=lambda r: -r["src_files"])
    for c in code_only:
        lines.append(f"- `{c['area']}/{c['slug']}` — {c['src_files']} src file(s), "
                     f"{c['tests']} test(s), 0 figs, 0 metrics [{c['bucket']}]")
    lines += [
        "",
        "## Per-paper table (all not-reproduced candidates)",
        "",
        "| Slug | Area | Bucket | Intrinsic | Evidence |",
        "|---|---|---|---|---|",
    ]
    for c in sorted(candidates, key=lambda r: (r["bucket"], r["area"], r["slug"])):
        ev = c.get("evidence", "").replace("|", "\\|")[:170]
        lines.append(f"| {c['slug']} | {c['area']} | {c['bucket']} | "
                     f"{c.get('intrinsic_signal', '-')} | {ev} |")
    if flags:
        lines += ["", "## Uncertain items (kept-on-doubt / newer than snapshot)", ""]
        for f in flags:
            lines.append(f"- {f}")
    lines.append("")
    (docs / "FAILURE_ANALYSIS.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {docs / 'FAILURE_ANALYSIS.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
