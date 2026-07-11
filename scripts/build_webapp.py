#!/usr/bin/env python3
"""
Static web app generator for the AI / DS / ML / DL reproduction researcher.

Walks the repo's <AREA>/<paper-slug>/ folders and emits a self-contained,
interactive, dark "scientific" site under <repo>/webapp/:

  index.html            dashboard: area filter + full-text search + status filter
  papers/<id>.html      per-paper page (tabbed): embeds the ORIGINAL paper PDF,
                        the summary.pdf, ORIGINAL vs REPRODUCED figures side by
                        side, the src file tree, unit-test status, and the Manim
                        animation (HTML5 <video> / <img> gif).
  assets/style.css      scientific styling (dark theme, responsive)
  assets/app.js         client-side filter/search + tabs + figure lightbox
  data/status.json      machine-readable build summary (for the QA agent)

Canonical per-paper layout it understands (all optional; rendered when present):

  <AREA>/<slug>/
    <original paper>.pdf   the ORIGINAL paper  (paper.pdf | original.pdf | *.pdf)
    summary.pdf            concise methodology / reproduction write-up
    src/                   reproduced Python code
    original_data/         authors' data  (+ optional DATA_SOURCE.md)
    original_results/      the paper's key figures            (ORIGINAL column)
    reproduced_results/    figures/metrics produced by src/   (REPRODUCED column)
    tests/                 pytest unit tests (test_*.py)
    manim/                 manim animation(s): .mp4 / .webm / .gif
    REPRODUCTION.md        (legacy) reproduction notes, rendered if present

Backward compatible with the older reproduce.py scaffold
(results/ + figures/ + REPRODUCTION.md + paper.pdf).

Besides the "Paper Reproductions" branch above, the site renders a second
top-level branch, "Project Reproductions": GitHub projects reproduced under
the UnifiedML two-track protocol. Dossiers are discovered read-only under
<UNIFIEDML_ROOT>/projects/<slug>/ (REPRODUCTION_CONTRACT.md marks a dossier)
and rendered as projects/<slug>.html with two clearly separated tracks:

  Track A  faithful reference (pinned upstream, unmodified, sandboxed) --
           blocked statuses (BLOCKED_BY_*) are first-class results, rendered
           with the recorded WHY and what was attempted.
  Track B  clean-room reimplementation on UnifiedML APIs -- real gate
           metrics read from the dossier JSONs at build time.

A "modernized" run (when recorded) renders in a visually distinct callout,
never inside either track: modernized results are NOT a reproduction.
Statuses are shown ONLY when an explicit status string from the frozen
reproduction vocabulary exists in an artifact or in the lab's
research/PILOT_REVIEW.md verdicts table; nothing is inferred or upgraded.
If the UnifiedML root is absent/unreadable the branch degrades to
"no project reproductions available" and never breaks the paper build.

No build tooling required. Open webapp/index.html directly, or run

    python build_webapp.py --serve

to launch a local HTTP server (recommended so embedded PDFs resolve cleanly),
optionally with --run-tests to execute each paper's pytest suite and surface a
pass/fail badge on its page.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
AREAS = {"AI": "Artificial Intelligence", "DS": "Data Science",
         "ML": "Machine Learning", "DL": "Deep Learning"}
# "Computational Observatory" area accent colors
# DS uses sky blue (distinct from the cyan --accent2 used for links/buttons) so
# area colour carries information on a DS-heavy corpus.
AREA_COLORS = {"AI": "#A78BFA", "DS": "#38BDF8", "ML": "#34D399", "DL": "#FBBF24"}
# glyphs used for gradient poster placeholders / hero accents
AREA_GLYPHS = {"AI": "✶", "DS": "⬡", "ML": "△", "DL": "◈"}
# Visitor-facing status labels — machine run_status values are for filtering only,
# never shown raw ("timeout"/"completed"/"error" would scare a portfolio visitor).
STATUS_LABEL = {"timeout": "pending", "completed": "reproduced",
                "running": "in progress", "pending": "pending", "error": "pending"}

# Google Fonts + preconnect, shared across every emitted page.
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Spectral:ital,wght@0,400;0,600;0,700;0,800;1,400&'
    'family=Orbitron:wght@600;700&'
    'family=Inter:wght@400;500;600;700&'
    'family=JetBrains+Mono:wght@400;500;600&display=swap">'
)

IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")
VID_EXTS = (".mp4", ".webm", ".mov", ".m4v")
GIF_EXTS = (".gif",)
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ipynb_checkpoints", ".git", "node_modules"}

esc = html.escape


# --------------------------------------------------------------------------- #
# tiny markdown -> html (dependency free)
# --------------------------------------------------------------------------- #
def _split_row(row: str) -> list[str]:
    """Split a markdown table row into trimmed cells, tolerating optional
    leading/trailing pipes."""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in row.split("|")]


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_code = in_ul = in_ol = False
    n = len(lines)
    i = 0

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code
        if stripped.startswith("```"):
            close_lists()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            i += 1
            continue
        if in_code:
            out.append(esc(line))
            i += 1
            continue

        # horizontal rule: 3+ of -, * or _ (spaces allowed between)
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        # GitHub-style table: a pipe row immediately followed by a |---|:--:| separator
        if "|" in line and i + 1 < n and "|" in lines[i + 1] \
                and re.match(r"^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$", lines[i + 1]):
            close_lists()
            header = _split_row(line)
            out.append("<table><thead><tr>"
                       + "".join(f"<th>{inline(c)}</th>" for c in header)
                       + "</tr></thead><tbody>")
            i += 2  # consume header + separator
            while i < n and "|" in lines[i] and lines[i].strip():
                cells = _split_row(lines[i])
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        # unordered list
        if re.match(r"^\s*[-*+]\s+", line):
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append("<li>" + inline(re.sub(r"^\s*[-*+]\s+", "", line)) + "</li>")
            i += 1
            continue

        # ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append("<li>" + inline(re.sub(r"^\s*\d+\.\s+", "", line)) + "</li>")
            i += 1
            continue

        close_lists()

        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            # Offset authored headings below the panel's section h3 so a leading
            # '# Title' becomes an <h3>, not a duplicate/inverted <h1>.
            lvl = min(len(m.group(1)) + 2, 6)
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        elif stripped:
            out.append("<p>" + inline(line) + "</p>")
        i += 1

    close_lists()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"(?<![\">])(https?://[^\s<]+)",
               r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
    # italics LAST so they can't corrupt URLs/code already emitted above:
    #   *text*  (single star, not part of **bold**) and _text_ (not intra-word)
    s = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\w\\])_([^_\n]+?)_(?![\w])", r"<em>\1</em>", s)
    return s


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def load_progress(repo: Path) -> dict[str, dict[str, Any]]:
    p = repo / "state" / "progress.jsonl"
    by_slug: dict[str, dict[str, Any]] = {}
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                by_slug[r["slug"]] = r  # last wins
            except Exception:  # noqa: BLE001
                pass
    return by_slug


def _images(d: Path) -> list[Path]:
    if not d.is_dir():
        return []
    return sorted(p for p in d.rglob("*")
                  if p.is_file() and p.suffix.lower() in IMG_EXTS
                  and not any(part in SKIP_DIRS for part in p.parts))


def _first_dir(base: Path, names: list[str]) -> Path | None:
    for n in names:
        if (base / n).is_dir():
            return base / n
    return None


def _original_pdf(d: Path) -> Path | None:
    for name in ("paper.pdf", "original.pdf"):
        if (d / name).exists():
            return d / name
    # any top-level pdf that isn't the summary
    for p in sorted(d.glob("*.pdf")):
        if p.name.lower() != "summary.pdf":
            return p
    return None


def _read_meta(d: Path, prog: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for name in ("metadata.json", "paper.json"):
        f = d / name
        if f.exists():
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                break
            except Exception:  # noqa: BLE001
                pass
    return meta or prog.get("meta", {}) or {}


def _notes(d: Path) -> tuple[str, Path | None]:
    for name in ("REPRODUCTION.md", "reproduction.md", "SUMMARY.md", "summary.md",
                 "README.md", "NOTES.md"):
        f = d / name
        if f.exists():
            return f.read_text(encoding="utf-8", errors="replace"), f
    return "", None


def _load_json(path: Path | None) -> Any:
    """Best-effort JSON load; never raises."""
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None


def load_captions(dir_: Path | None) -> dict[str, str]:
    """Load figure captions from <dir>/captions.json when present.

    Tolerant of several shapes so the unattended pipeline can emit whatever is
    convenient:
      {"fig1.png": "caption", ...}
      {"captions": {"fig1.png": "caption"}}
      {"figures": [{"file": "fig1.png", "caption": "..."}]}
      [{"file": "fig1.png", "caption": "..."}]
    Keys are indexed by both basename and stem for forgiving lookups.
    """
    caps: dict[str, str] = {}
    if not dir_:
        return caps
    data = _load_json(dir_ / "captions.json")
    if data is None:
        return caps

    def _put(fname: Any, cap: Any) -> None:
        if not fname or cap is None:
            return
        text = str(cap).strip()
        if not text:
            return
        base = os.path.basename(str(fname))
        caps[base] = text
        stem = os.path.splitext(base)[0]
        caps.setdefault(stem, text)

    try:
        if isinstance(data, dict):
            inner = data.get("captions") if isinstance(data.get("captions"), dict) else None
            figs = data.get("figures")
            if inner:
                for k, v in inner.items():
                    _put(k, v)
            if isinstance(figs, list):
                for item in figs:
                    if isinstance(item, dict):
                        _put(item.get("file") or item.get("name") or item.get("path"),
                             item.get("caption") or item.get("text") or item.get("title"))
            if not inner and not figs:
                for k, v in data.items():
                    if isinstance(v, str):
                        _put(k, v)
                    elif isinstance(v, dict):
                        _put(k, v.get("caption") or v.get("text"))
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    _put(item.get("file") or item.get("name") or item.get("path"),
                         item.get("caption") or item.get("text") or item.get("title"))
    except Exception:  # noqa: BLE001
        return caps
    return caps


def caption_for(f: Path, caps: dict[str, str]) -> str:
    """Look up a caption for a figure by basename then stem."""
    if not caps:
        return ""
    return caps.get(f.name) or caps.get(f.stem) or ""


def load_metrics(paper_dir: Path, repro_dir: Path | None) -> Any:
    """Load reproduced metrics.json (or a results.json fallback). Never raises."""
    candidates: list[Path] = []
    if repro_dir:
        candidates += [repro_dir / "metrics.json", repro_dir / "results.json",
                       repro_dir / "results" / "results.json"]
    candidates += [paper_dir / "reproduced_results" / "metrics.json",
                   paper_dir / "results" / "results.json",
                   paper_dir / "metrics.json"]
    seen: set[str] = set()
    for c in candidates:
        key = str(c)
        if key in seen:
            continue
        seen.add(key)
        data = _load_json(c)
        if data is not None:
            return data
    return None


def repro_verdict(p: dict[str, Any], metrics: Any) -> dict[str, Any]:
    """Automated reproducibility heuristic -> full / partial / minimal.

    Transparent and evidence-backed: combines confirmed central claims, test
    outcomes, presence of reproduced figures, and captured metrics into a
    weighted ratio. Purely a heuristic and labelled as such in the UI.
    """
    m = metrics if isinstance(metrics, dict) else {}
    score = 0.0
    weight = 0.0
    signals: list[dict[str, str]] = []

    claim_items: list[tuple[str, bool]] = []
    claims = m.get("central_claims")
    if isinstance(claims, dict):
        claim_items = [(str(k), bool(v)) for k, v in claims.items() if isinstance(v, bool)]
    if claim_items:
        n_ok = sum(1 for _, v in claim_items if v)
        r = n_ok / len(claim_items)
        score += r * 3.0
        weight += 3.0
        st = "ok" if r >= 0.75 else "warn" if r >= 0.34 else "bad"
        signals.append({"state": st,
                        "text": f"{n_ok}/{len(claim_items)} central claims confirmed"})

    ts = p.get("tstat") or {}
    if ts.get("ran"):
        ok = ts.get("failed", 0) == 0 and ts.get("errors", 0) == 0 and ts.get("passed", 0) > 0
        score += 2.0 if ok else 0.0
        weight += 2.0
        total = ts.get("total") or ts.get("passed", 0)
        signals.append({"state": "ok" if ok else "bad",
                        "text": f"{ts.get('passed', 0)}/{total} tests passing"})
    elif p.get("tests"):
        signals.append({"state": "warn",
                        "text": f"{len(p['tests'])} test file(s) present (not run)"})

    if p.get("repro_figs"):
        score += 1.0
        weight += 1.0
        signals.append({"state": "ok",
                        "text": f"{len(p['repro_figs'])} reproduced figure(s)"})
    else:
        signals.append({"state": "warn", "text": "no reproduced figures"})

    if m:
        score += 1.0
        weight += 1.0
        signals.append({"state": "ok", "text": "metrics.json captured"})
    else:
        signals.append({"state": "warn", "text": "no metrics.json"})

    # Divide by at least 2.0 so a sub-threshold single-signal paper maps to ~0.5
    # rather than a misleading 100% ring. For weight>=2 this is a no-op, so no
    # genuine "full" is demoted and the verdict branches below are unaffected.
    ratio = (score / max(weight, 2.0)) if weight else 0.0
    # "full" needs strong agreement AND at least two independent positive
    # signals — a single reproduced artifact alone stays "partial".
    if weight == 0:
        verdict = "minimal"
    elif ratio >= 0.75 and weight >= 2.0:
        verdict = "full"
    elif ratio >= 0.40:
        verdict = "partial"
    else:
        verdict = "minimal"
    return {"verdict": verdict, "ratio": round(ratio, 2),
            "signals": signals, "claims": claim_items}


def discover(repo: Path, run_tests: bool, py_exe: str | None) -> list[dict[str, Any]]:
    progress = load_progress(repo)
    papers: list[dict[str, Any]] = []
    for code in AREAS:
        area_dir = repo / code
        if not area_dir.is_dir():
            continue
        for d in sorted(area_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            prog = progress.get(d.name, {})
            meta = dict(_read_meta(d, prog))  # copy so link injection is local
            # surface the discovered code repo as a "Code" link when metadata
            # didn't already carry one
            gh = prog.get("github_repo")
            if gh and not meta.get("code_url"):
                meta["code_url"] = gh if str(gh).startswith("http") else f"https://github.com/{gh}"

            orig_dir = _first_dir(d, ["original_results", "original_figures", "original"])
            repro_dir = _first_dir(d, ["reproduced_results", "results", "figures", "repro_results"])
            orig_figs = _images(orig_dir) if orig_dir else []
            repro_figs = _images(repro_dir) if repro_dir else []
            # avoid double-counting if repro fell back to a dir that is also 'figures'
            src_root = (d / "src") if (d / "src").is_dir() else None
            src_files = sorted(p for p in src_root.rglob("*")
                               if src_root and p.is_file()
                               and not any(part in SKIP_DIRS for part in p.parts)) if src_root else []
            tests_dir = (d / "tests") if (d / "tests").is_dir() else None
            tests = sorted(tests_dir.glob("test_*.py")) if tests_dir else []
            manim_dir = (d / "manim") if (d / "manim").is_dir() else None
            # Only surface finished animations: skip Manim's internal caches
            # (_media/, partial_movie_files/, uncached_*, __pycache__), which
            # otherwise flood the player with dozens of fragment clips.
            _manim_skip = {"_media", "partial_movie_files", "__pycache__",
                           "images", "texts"}
            manim_media = sorted(
                p for p in manim_dir.rglob("*")
                if p.is_file()
                and p.suffix.lower() in (VID_EXTS + GIF_EXTS)
                and not any(part in _manim_skip for part in p.parts)
                and not p.name.lower().startswith(("uncached_", "partial_"))
            ) if manim_dir else []
            data_dir = _first_dir(d, ["original_data", "data"])
            data_files = sorted(p for p in data_dir.rglob("*")
                                if p.is_file()) if data_dir else []
            data_src_md = None
            if data_dir:
                for name in ("DATA_SOURCE.md", "DATA.md", "SOURCE.md"):
                    if (data_dir / name).exists():
                        data_src_md = data_dir / name
                        break

            notes_md, notes_file = _notes(d)
            summary_pdf = (d / "summary.pdf") if (d / "summary.pdf").exists() else None
            orig_pdf = _original_pdf(d)

            orig_caps = load_captions(orig_dir)
            repro_caps = load_captions(repro_dir)
            metrics = load_metrics(d, repro_dir)

            tstat = run_pytest(d, tests_dir, py_exe) if (run_tests and tests) else \
                {"ran": False, "passed": 0, "failed": 0, "errors": 0, "total": len(tests), "summary": ""}

            title = meta.get("title") or prog.get("title") or d.name.replace("-", " ").title()
            produced = bool(prog.get("produced")) or bool(repro_figs) or bool(
                (repro_dir and (repro_dir.parent / "results" / "results.json").exists()))
            status = prog.get("run_status", "reproduced" if produced else "pending")

            rec = {
                "code": code, "slug": d.name, "dir": d, "title": title,
                "meta": meta,
                "orig_pdf": orig_pdf, "summary_pdf": summary_pdf,
                "orig_figs": orig_figs, "repro_figs": repro_figs,
                "orig_caps": orig_caps, "repro_caps": repro_caps,
                "metrics": metrics,
                "src": src_files, "src_root": src_root,
                "tests": tests, "tests_dir": tests_dir, "tstat": tstat,
                "manim": manim_media, "manim_dir": manim_dir,
                "data_files": data_files, "data_dir": data_dir, "data_src_md": data_src_md,
                "notes": notes_md, "notes_file": notes_file,
                "status": status, "produced": produced,
                "elapsed_s": prog.get("elapsed_s"),
            }
            try:
                rec["verdict"] = repro_verdict(rec, metrics)
            except Exception:  # noqa: BLE001
                rec["verdict"] = {"verdict": "minimal", "ratio": 0.0,
                                  "signals": [], "claims": []}
            papers.append(rec)
    return papers


def run_pytest(paper_dir: Path, tests_dir: Path | None, py_exe: str | None) -> dict[str, Any]:
    """Run the paper's pytest suite (best effort) and parse a pass/fail summary."""
    res = {"ran": False, "passed": 0, "failed": 0, "errors": 0, "total": 0, "summary": ""}
    if not tests_dir or not py_exe or not Path(py_exe).exists():
        return res
    try:
        proc = subprocess.run(
            [py_exe, "-m", "pytest", str(tests_dir), "-q", "--no-header",
             "-o", "addopts="],
            cwd=str(paper_dir), capture_output=True, text=True, timeout=180,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        for key, pat in (("passed", r"(\d+) passed"), ("failed", r"(\d+) failed"),
                         ("errors", r"(\d+) error")):
            m = re.search(pat, out)
            if m:
                res[key] = int(m.group(1))
        res["total"] = res["passed"] + res["failed"] + res["errors"]
        tail = [ln for ln in out.splitlines() if ln.strip()]
        # "ran" only if pytest actually collected/ran something
        parsed = res["total"] > 0 or "no tests ran" in out.lower()
        if "no module named pytest" in out.lower():
            res["ran"] = False
            res["summary"] = "pytest not installed in this environment"
        elif parsed:
            res["ran"] = True
            res["summary"] = tail[-1] if tail else ""
        else:
            res["ran"] = False
            res["summary"] = tail[-1] if tail else "pytest produced no parseable summary"
    except subprocess.TimeoutExpired:
        res["summary"] = "pytest timed out (>180s)"
    except Exception as exc:  # noqa: BLE001
        res["summary"] = f"pytest could not run: {exc}"
    return res


def rel(from_file: Path, target: Path) -> str:
    return os.path.relpath(target, from_file.parent).replace("\\", "/")


# --------------------------------------------------------------------------- #
# per-paper rendering
# --------------------------------------------------------------------------- #
def render_tree(root: Path, page: Path) -> str:
    def walk(dir_: Path) -> str:
        items = ""
        entries = sorted(dir_.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        for e in entries:
            if e.name in SKIP_DIRS or e.name.endswith(".pyc"):
                continue
            if e.is_dir():
                inner = walk(e)
                if inner:
                    items += (f'<li class="dir"><details open><summary>{esc(e.name)}/'
                              f'</summary>{inner}</details></li>')
            else:
                items += f'<li class="file"><a href="{rel(page, e)}">{esc(e.name)}</a></li>'
        return f"<ul class=tree>{items}</ul>" if items else ""
    return walk(root) or '<p class="muted">No source files yet.</p>'


def render_pdf(page: Path, pdf: Path | None, label: str) -> str:
    if not pdf:
        return f'<p class="muted">{esc(label)} not available yet.</p>'
    href = rel(page, pdf)
    return (
        f'<div class="pdf-wrap"><div class="pdf-bar">'
        f'<span class="mono">{esc(pdf.name)}</span>'
        f'<a class="btn" href="{href}" target="_blank" rel="noopener">Open ↗</a></div>'
        f'<iframe class="pdf" src="{href}#view=FitH" title="{esc(label)}" loading="lazy"></iframe>'
        f'</div>'
    )


def _figcap(f: Path, caps: dict[str, str]) -> str:
    """Caption block: prose caption (when present) + filename in mono."""
    text = caption_for(f, caps)
    prose = f'<span class="capline">{inline(text)}</span>' if text else ""
    return (f'<figcaption class="figcap">{prose}'
            f'<span class="mono muted small">{esc(f.name)}</span></figcaption>')


def render_figs_compare(page: Path, orig: list[Path], repro: list[Path],
                        orig_caps: dict[str, str] | None = None,
                        repro_caps: dict[str, str] | None = None) -> str:
    orig_caps = orig_caps or {}
    repro_caps = repro_caps or {}
    if not orig and not repro:
        return '<p class="muted">No figures yet.</p>'
    n = max(len(orig), len(repro))
    rows = []
    for i in range(n):
        o = orig[i] if i < len(orig) else None
        r = repro[i] if i < len(repro) else None

        def cell(f: Path | None, tag: str, caps: dict[str, str]) -> str:
            if not f:
                return (f'<div class="figcell empty"><span class="tag">{tag}</span>'
                        f'<div class="noimg">— none —</div></div>')
            src = rel(page, f)
            data_cap = esc(caption_for(f, caps))
            alt = esc(caption_for(f, caps) or f.name)
            return (f'<div class="figcell"><span class="tag">{tag}</span>'
                    f'<img loading="lazy" decoding="async" src="{src}" alt="{alt}" '
                    f'data-full="{src}" data-cap="{data_cap}" class="zoom" '
                    f'tabindex="0" role="button" aria-label="Enlarge figure: {alt}">'
                    f'{_figcap(f, caps)}</div>')
        rows.append(f'<div class="figrow">{cell(o, "ORIGINAL", orig_caps)}'
                    f'{cell(r, "REPRODUCED", repro_caps)}</div>')
    return "".join(rows)


def render_gallery(page: Path, figs: list[Path], empty: str,
                   caps: dict[str, str] | None = None) -> str:
    caps = caps or {}
    if not figs:
        return f'<p class="muted">{esc(empty)}</p>'
    cells = []
    for f in figs:
        src = rel(page, f)
        data_cap = esc(caption_for(f, caps))
        alt = esc(caption_for(f, caps) or f.name)
        cells.append(
            f'<figure class="gcell"><img loading="lazy" decoding="async" src="{src}" '
            f'alt="{alt}" data-full="{src}" data-cap="{data_cap}" class="zoom" '
            f'tabindex="0" role="button" aria-label="Enlarge figure: {alt}">'
            f'{_figcap(f, caps)}</figure>')
    return f'<div class="gallery">{"".join(cells)}</div>'


# --------------------------------------------------------------------------- #
# metrics table + reproducibility analysis
# --------------------------------------------------------------------------- #
def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _fmt_scalar(v: Any) -> str:
    if v is None:
        return '<span class="muted">null</span>'
    if isinstance(v, bool):
        return (f'<span class="mbool {"y" if v else "n"}">'
                f'{"✓" if v else "✗"} {str(v).lower()}</span>')
    if isinstance(v, float):
        a = abs(v)
        if v == int(v) and a < 1e15:
            s = f"{int(v)}"
        elif a != 0 and (a < 1e-3 or a >= 1e5):
            s = f"{v:.3e}"
        else:
            s = f"{v:.4g}"
        return f'<span class="mnum">{esc(s)}</span>'
    if isinstance(v, int):
        return f'<span class="mnum">{v}</span>'
    return esc(str(v))


def _render_matrix(d: dict[str, Any]) -> str:
    cols: list[str] = []
    for row in d.values():
        for k in row:
            if k not in cols:
                cols.append(k)
    cols = cols[:16]
    thead = "<tr><th></th>" + "".join(f"<th>{esc(str(c))}</th>" for c in cols) + "</tr>"
    body = ""
    for name, row in d.items():
        cells = "".join(
            f"<td>{_fmt_scalar(row.get(c)) if c in row else '<span class=muted>·</span>'}</td>"
            for c in cols)
        body += f"<tr><th>{esc(str(name))}</th>{cells}</tr>"
    return f'<div class="mtab-wrap"><table class="mtab matrix">{thead}{body}</table></div>'


def _render_node(v: Any, depth: int) -> str:
    if _is_scalar(v):
        return _fmt_scalar(v)
    if isinstance(v, list):
        if all(_is_scalar(x) for x in v):
            inner = ", ".join(re.sub(r"<[^>]+>", "", _fmt_scalar(x)) for x in v)
            return f'<span class="mono mlist">[{esc(inner)}]</span>'
        if all(isinstance(x, dict) for x in v):
            # list of records -> matrix keyed by index
            return _render_matrix({str(i): x for i, x in enumerate(v)})
        return "".join(f'<div class="mrow">{_render_node(x, depth + 1)}</div>' for x in v)
    if isinstance(v, dict):
        if not v:
            return '<span class="muted small">{}</span>'
        if all(_is_scalar(x) for x in v.values()):
            rows = "".join(f"<tr><th>{esc(str(k))}</th><td>{_fmt_scalar(val)}</td></tr>"
                           for k, val in v.items())
            return f'<div class="mtab-wrap"><table class="mtab">{rows}</table></div>'
        if all(isinstance(x, dict) and x and all(_is_scalar(z) for z in x.values())
               for x in v.values()):
            return _render_matrix(v)
        parts = []
        for k, val in v.items():
            if _is_scalar(val):
                parts.append(f'<div class="mrow"><span class="mk">{esc(str(k))}</span>'
                             f'{_fmt_scalar(val)}</div>')
            else:
                open_attr = " open" if depth < 1 else ""
                parts.append(f'<details class="msec"{open_attr}>'
                             f'<summary>{esc(str(k))}</summary>'
                             f'<div class="msec-body">{_render_node(val, depth + 1)}</div>'
                             f'</details>')
        return "".join(parts)
    return esc(str(v))


def render_metrics(metrics: Any) -> str:
    if metrics is None:
        return ('<p class="muted">No <code>metrics.json</code> was captured for this '
                'reproduction yet.</p>')
    try:
        return f'<div class="metrics">{_render_node(metrics, 0)}</div>'
    except Exception:  # noqa: BLE001
        return '<p class="muted">metrics.json present but could not be rendered.</p>'


VERDICT_LABEL = {"full": "Full reproduction", "partial": "Partial reproduction",
                 "minimal": "Minimal reproduction"}
VERDICT_CLASS = {"full": "ok", "partial": "warn", "minimal": "bad"}
VERDICT_BLURB = {
    "full": "Central claims, tests, figures and metrics line up with the source paper.",
    "partial": "Some evidence reproduced; one or more claims, tests, or artifacts are missing or diverge.",
    "minimal": "Limited evidence reproduced so far — treat results as preliminary.",
}


def render_analysis(p: dict[str, Any]) -> str:
    v = p.get("verdict") or {"verdict": "minimal", "ratio": 0.0, "signals": [], "claims": []}
    verdict = v.get("verdict", "minimal")
    cls = VERDICT_CLASS.get(verdict, "bad")
    pct = int(round(float(v.get("ratio", 0.0)) * 100))

    signals = ""
    for s in v.get("signals", []):
        st = s.get("state", "warn")
        signals += (f'<li class="sig {esc(st)}"><span class="dot {esc(st)}"></span>'
                    f'{esc(s.get("text", ""))}</li>')
    signals_html = f'<ul class="siglist">{signals}</ul>' if signals else ""

    claims = v.get("claims") or []
    claims_html = ""
    if claims:
        chips = "".join(
            f'<span class="claim {"y" if ok else "n"}">'
            f'{"✓" if ok else "✗"} {esc(name)}</span>' for name, ok in claims)
        claims_html = (f'<h3>Central claims</h3><div class="claims">{chips}</div>')

    header = (
        f'<div class="verdict {cls}">'
        f'<div class="verdict-main">'
        f'<span class="verdict-tag">Reproducibility verdict</span>'
        f'<span class="verdict-word">{esc(VERDICT_LABEL.get(verdict, verdict))}</span>'
        f'<span class="verdict-blurb">{esc(VERDICT_BLURB.get(verdict, ""))}</span>'
        f'</div>'
        f'<div class="verdict-meter" title="heuristic evidence score">'
        f'<svg viewBox="0 0 36 36" class="ring"><path class="ring-bg" '
        f'd="M18 2.5a15.5 15.5 0 1 1 0 31 15.5 15.5 0 0 1 0-31"/>'
        f'<path class="ring-fg" stroke-dasharray="{pct},100" '
        f'd="M18 2.5a15.5 15.5 0 1 1 0 31 15.5 15.5 0 0 1 0-31"/></svg>'
        f'<span class="ring-num">{pct}<small>%</small></span></div>'
        f'</div>'
        f'<p class="muted small">Automated heuristic from the evidence below — not a '
        f'human judgement.</p>'
    )
    ev = f'<h3>Evidence</h3>{signals_html}' if signals_html else ""
    metrics_html = f'<h3>Metrics</h3>{render_metrics(p.get("metrics"))}'
    return header + claims_html + ev + metrics_html


def render_manim(page: Path, media: list[Path], title: str, code: str,
                 poster: Path | None, produced: bool = False) -> str:
    """Cinematic framed player: title above, framed <video>/<img> with an accent
    glow, a caption below, and a filmstrip when multiple animations exist."""
    if not media:
        return ('<div class="anim-empty">'
                f'<div class="anim-glyph" style="--c:{AREA_COLORS[code]}">'
                f'{AREA_GLYPHS[code]}</div>'
                '<p class="muted">No animation was rendered for this paper yet.</p>'
                '<p class="muted small">The pipeline produces a Manim clip of the core '
                'finding once reproduction succeeds.</p></div>')

    lead = "Reproduced finding" if produced else "Animation"

    def player(f: Path) -> str:
        src = rel(page, f)
        poster_attr = f' poster="{rel(page, poster)}"' if poster else ""
        if f.suffix.lower() in GIF_EXTS:
            media_el = (f'<img class="anim-media" src="{src}" alt="{esc(f.name)}" '
                        f'loading="lazy">')
        else:
            media_el = (
                f'<video class="anim-media" controls loop muted playsinline '
                f'preload="metadata"{poster_attr}>'
                f'<source src="{src}">Your browser cannot play this video. '
                f'<a href="{src}">Download {esc(f.name)}</a>.</video>')
        return (
            f'<figure class="cinema" data-src="{src}">'
            f'<div class="cinema-frame">{media_el}</div>'
            f'<figcaption class="cinema-cap">{esc(lead)} — '
            f'{esc(AREAS[code])}<span class="mono muted"> · {esc(f.name)}</span>'
            f'</figcaption></figure>')

    stage = f'<div class="cinema-stage" id="cinema-stage">{player(media[0])}</div>'
    strip = ""
    if len(media) > 1:
        cells = []
        for i, f in enumerate(media):
            src = rel(page, f)
            cells.append(
                f'<button class="film-cell{" active" if i == 0 else ""}" '
                f'data-src="{src}" data-gif="{"1" if f.suffix.lower() in GIF_EXTS else "0"}" '
                f'data-name="{esc(f.name)}" data-area="{esc(AREAS[code])}" '
                f'data-lead="{esc(lead)}">'
                f'<span class="film-idx mono">{i + 1:02d}</span>'
                f'<span class="film-name mono">{esc(f.name)}</span></button>')
        strip = (f'<div class="filmstrip">{"".join(cells)}</div>')
    return f'<div class="cinema-wrap">{stage}{strip}</div>'


def render_tests(tstat: dict[str, Any], tests: list[Path], page: Path) -> str:
    lst = "".join(f"<li><code>{esc(rel(page, t))}</code></li>" for t in tests) or \
        "<li class=muted>no tests yet</li>"
    if tstat.get("ran"):
        ok = tstat["failed"] == 0 and tstat["errors"] == 0 and tstat["passed"] > 0
        cls = "pass" if ok else "fail"
        label = (f'{tstat["passed"]} passed' +
                 (f', {tstat["failed"]} failed' if tstat["failed"] else "") +
                 (f', {tstat["errors"]} errors' if tstat["errors"] else ""))
        badge = f'<span class="tbadge {cls}">{"PASS" if ok else "FAIL"} · {label}</span>'
        summ = f'<pre class="mono small">{esc(tstat.get("summary", ""))}</pre>' if tstat.get("summary") else ""
    else:
        badge = (f'<span class="tbadge na">{len(tests)} test file(s) · not run</span>'
                 if tests else '<span class="tbadge na">no tests</span>')
        summ = '<p class="muted small">Run <code>build_webapp.py --serve --run-tests</code> to execute.</p>' \
            if tests else ""
    return f'{badge}{summ}<ul class="filelist">{lst}</ul>'


def render_data(data_files: list[Path], data_dir: Path | None,
                data_src_md: Path | None, page: Path) -> str:
    if not data_dir:
        return '<p class="muted">No original data folder.</p>'
    parts = []
    if data_src_md:
        parts.append(md_to_html(data_src_md.read_text(encoding="utf-8", errors="replace")))
    real = [f for f in data_files if f.name not in
            ("DATA_SOURCE.md", "DATA.md", "SOURCE.md", ".gitkeep")]
    if real:
        shown = real[:40]
        lst = "".join(f"<li><code>{esc(f.name)}</code> "
                      f'<span class="muted small">{f.stat().st_size:,} B</span></li>'
                      for f in shown)
        more = f'<li class="muted">+{len(real) - len(shown)} more…</li>' if len(real) > len(shown) else ""
        parts.append(f'<ul class="filelist">{lst}{more}</ul>')
    elif not data_src_md:
        parts.append('<p class="muted">Folder present but empty.</p>')
    return "".join(parts)


def tab(name: str, key: str, active: bool, count: int | None = None) -> str:
    # count is None -> no badge (Overview/Analysis/Summary/Data); an int (incl. 0)
    # renders a badge, and 0 dims the tab so an empty artifact reads as empty.
    empty = count == 0
    badge = f' <span class="tabcount">{count}</span>' if count is not None else ""
    cls = "tab" + (" active" if active else "") + (" tab-empty" if empty else "")
    return (f'<button class="{cls}" role="tab" id="tab-{key}" '
            f'aria-controls="panel-{key}" '
            f'aria-selected="{"true" if active else "false"}" '
            f'data-tab="{key}">{esc(name)}{badge}</button>')


def panel(key: str, active: bool, inner: str) -> str:
    return (f'<section class="panel{" active" if active else ""}" role="tabpanel" '
            f'id="panel-{key}" aria-labelledby="tab-{key}" tabindex="0" '
            f'data-panel="{key}">{inner}</section>')


def render_paper(p: dict[str, Any], web: Path) -> Path:
    page = web / "papers" / f"{p['code']}-{p['slug']}.html"
    color = AREA_COLORS[p["code"]]
    meta = p["meta"]

    # meta line: authors / year / venue / links
    bits = []
    if meta.get("authors"):
        auth = meta["authors"]
        if isinstance(auth, list):
            auth = ", ".join(auth[:6]) + (" et al." if len(auth) > 6 else "")
        bits.append(esc(str(auth)))
    if meta.get("year"):
        bits.append(esc(str(meta["year"])))
    if meta.get("venue"):
        bits.append(esc(str(meta["venue"])))
    metaline = " · ".join(bits)
    links = []
    for key, lbl in (("arxiv_id", "arXiv"), ("doi", "DOI"),
                     ("landing_url", "Source"), ("pdf_url", "PDF"), ("code_url", "Code")):
        v = meta.get(key)
        if not v:
            continue
        if key == "arxiv_id":
            url = f"https://arxiv.org/abs/{v}"
        elif key == "doi":
            url = f"https://doi.org/{v}"
        else:
            url = v
        links.append(f'<a class="ext" href="{esc(str(url))}" target="_blank" rel="noopener">{lbl} ↗</a>')
    linkbar = f'<div class="links">{"".join(links)}</div>' if links else ""

    abstract = ""
    if meta.get("abstract"):
        abstract = f'<div class="abstract">{inline(str(meta["abstract"]))}</div>'

    notes_html = md_to_html(p["notes"]) if p["notes"] else \
        '<p class="muted">No reproduction notes (REPRODUCTION.md) yet.</p>'

    # tabs — dossier order
    n_cmp = min(len(p["orig_figs"]), len(p["repro_figs"]))
    tabs = (
        tab("Overview", "overview", True) +
        tab("Analysis", "analysis", False) +
        tab("Reproduction summary", "summary", False) +
        tab("Original PDF", "paper", False) +
        tab("Reproduction", "repro", False, len(p["repro_figs"])) +
        tab("Compare", "compare", False, n_cmp) +
        tab("Animation", "anim", False, len(p["manim"])) +
        tab("Source", "source", False, len(p["src"])) +
        tab("Tests", "tests", False, len(p["tests"])) +
        tab("Data", "data", False)
    )

    overview = (
        (abstract or "") +
        '<h3>Reproduction notes</h3>' + notes_html
    )
    poster = p["repro_figs"][0] if p["repro_figs"] else (
        p["orig_figs"][0] if p["orig_figs"] else None)
    panels = (
        panel("overview", True, overview) +
        panel("analysis", False, render_analysis(p)) +
        panel("summary", False, render_pdf(page, p["summary_pdf"], "summary.pdf")) +
        panel("paper", False, render_pdf(page, p["orig_pdf"], "Original paper PDF")) +
        panel("repro", False,
              '<p class="muted small">Figures regenerated from scratch by '
              '<code>src/</code>. Click any figure to zoom.</p>' +
              render_gallery(page, p["repro_figs"],
                             "No reproduced figures yet.", p["repro_caps"])) +
        panel("compare", False,
              '<p class="muted small">Original figures from the paper (left) vs. figures '
              'regenerated by <code>src/</code> (right), paired where counts align. '
              'Captions come from <code>captions.json</code> when present.</p>' +
              render_figs_compare(page, p["orig_figs"], p["repro_figs"],
                                  p["orig_caps"], p["repro_caps"])) +
        panel("anim", False, render_manim(page, p["manim"], p["title"], p["code"],
                                          poster, p["produced"])) +
        panel("source", False, render_tree(p["src_root"], page) if p["src_root"]
              else '<p class="muted">No src/ folder yet.</p>') +
        panel("tests", False, render_tests(p["tstat"], p["tests"], page)) +
        panel("data", False, render_data(p["data_files"], p["data_dir"], p["data_src_md"], page))
    )

    status_pill = "reproduced" if p["produced"] else \
        esc(STATUS_LABEL.get(str(p["status"]), str(p["status"])))
    dot = "ok" if p["produced"] else "warn"
    vd = (p.get("verdict") or {}).get("verdict", "minimal")
    vcls = VERDICT_CLASS.get(vd, "bad")
    verdict_pill = (f'<a class="pill vpill {vcls}" href="#analysis" '
                    f'data-goto="analysis"><span class="dot {vcls}"></span>'
                    f'{esc(VERDICT_LABEL.get(vd, vd))}</a>')
    doc = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(p['title'])} — {p['code']}</title>
<link rel="icon" type="image/svg+xml" href="../assets/favicon.svg?v=3">
<link rel="icon" href="../assets/favicon.ico?v=3" sizes="any">
<link rel="apple-touch-icon" href="../assets/apple-touch-icon.png?v=3">
{FONTS}
<link rel=stylesheet href="../assets/style.css"></head>
<body class=paper>
<div class="bg-grid" aria-hidden="true"></div>
<header class="phead" style="--c:{color}">
  <a href="{rel(page, web / 'index.html')}" class="back">← all papers</a>
  <div class="phead-band">
    <span class="badge area" style="--c:{color}">{AREA_GLYPHS[p['code']]} {p['code']} · {esc(AREAS[p['code']])}</span>
    <h1>{esc(p['title'])}</h1>
    {f'<div class="metaline">{metaline}</div>' if metaline else ''}
    {linkbar}
    <div class="pills">
      <span class="pill {'ok' if p['produced'] else 'warn'}"><span class="dot {dot}"></span>{status_pill}</span>
      {verdict_pill}
      <span class="pill muted">{len(p['orig_figs'])} original · {len(p['repro_figs'])} reproduced figs</span>
      <span class="pill muted">{len(p['tests'])} tests</span>
      <span class="pill muted">{len(p['src'])} src files</span>
      {f'<span class="pill muted">{p["elapsed_s"]}s compute</span>' if p.get('elapsed_s') else ''}
    </div>
  </div>
</header>
<nav class="tabs" role=tablist>{tabs}</nav>
<main class="pbody">{panels}</main>
<div id=lightbox class=lightbox aria-hidden="true" role="dialog" aria-modal="true" aria-label="figure viewer">
  <button class="lb-close" id=lbclose aria-label="close">✕</button>
  <button class="lb-nav prev" id=lbprev aria-label="previous figure">‹</button>
  <figure class="lb-fig"><img id=lbimg src="" alt="zoomed figure">
    <figcaption id=lbcap class="lb-cap"></figcaption></figure>
  <button class="lb-nav next" id=lbnext aria-label="next figure">›</button>
</div>
<script src="../assets/app.js"></script>
</body></html>"""
    page.write_text(doc, encoding="utf-8")
    return page


# --------------------------------------------------------------------------- #
# dashboard rendering
# --------------------------------------------------------------------------- #
def render_card(p: dict[str, Any], web: Path, page: Path) -> str:
    idx = web / "index.html"
    color = AREA_COLORS[p["code"]]
    thumb_src = None
    if p["repro_figs"]:
        thumb_src = p["repro_figs"][0]
    elif p["orig_figs"]:
        thumb_src = p["orig_figs"][0]
    if thumb_src:
        thumb = (f'<img class="thumb" loading="lazy" decoding="async" '
                 f'src="{rel(idx, thumb_src)}" alt="">')
    else:
        thumb = (f'<div class="thumb placeholder" style="--c:{color}">'
                 f'<span class="poster-glyph">{AREA_GLYPHS[p["code"]]}</span></div>')
    # raw machine value stays on data-status for filtering; label is humanized
    status = "reproduced" if p["produced"] else str(p["status"] or "pending")
    status_label = "reproduced" if p["produced"] else \
        STATUS_LABEL.get(status, status)
    tstat = p["tstat"]
    tests_label = (f'{tstat["passed"]}/{tstat["total"]} tests' if tstat.get("ran") and tstat["total"]
                   else f'{len(p["tests"])} tests')
    has_anim = "yes" if p["manim"] else "no"
    dot = "ok" if p["produced"] else "warn"
    vd = (p.get("verdict") or {}).get("verdict", "minimal")
    vcls = VERDICT_CLASS.get(vd, "bad")
    verdict_chip = (f'<span class="vchip {vcls}" title="reproducibility verdict">'
                    f'{esc(vd)}</span>')
    flags = []
    if p["orig_pdf"]:
        flags.append("PDF")
    if p["manim"]:
        flags.append("anim")
    if p["summary_pdf"]:
        flags.append("summary")
    flagbar = "".join(f'<span class="tick">{f}</span>' for f in flags)
    return (
        f'<a class="card" data-code="{p["code"]}" data-status="{esc(status)}" '
        f'data-raw-status="{esc(str(p["status"] or "pending"))}" '
        f'data-anim="{has_anim}" data-verdict="{esc(vd)}" style="--c:{color}" '
        f'data-search="{esc((p["title"] + " " + p["code"] + " " + str(p["meta"].get("venue", ""))).lower())}" '
        f'href="{rel(idx, page)}">'
        f'<div class="thumb-wrap">{thumb}'
        f'<span class="badge area" style="--c:{color}">{AREA_GLYPHS[p["code"]]} {p["code"]}</span>'
        f'{verdict_chip}</div>'
        f'<div class="cbody">'
        f'<h3>{esc(p["title"])}</h3>'
        f'<div class="meta">'
        f'<span class="stat-dot"><span class="dot {dot}"></span>'
        f'{esc(status_label)}</span>'
        f'<span class="muted">{len(p["repro_figs"])} figs</span>'
        f'<span class="muted">{esc(tests_label)}</span>'
        f'</div>'
        f'<div class="ticks">{flagbar}</div>'
        f'</div></a>'
    )


HERO3D_JS = r"""/* Interactive neural-mesh sphere for the hero (Three.js from CDN). */
(function(){
  var el=document.getElementById('hero-orb');
  if(!el){return;}
  if(!window.THREE){el.innerHTML='<img src="assets/brand-emblem.png" alt="" width="110" height="110" style="border-radius:16px">';return;}
  var T=window.THREE;
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var size=el.clientWidth||150;
  var renderer=new T.WebGLRenderer({alpha:true,antialias:true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
  renderer.setSize(size,size);
  el.appendChild(renderer.domElement);
  var scene=new T.Scene();
  var camera=new T.PerspectiveCamera(45,1,0.1,100);
  camera.position.set(0,0,3.2);
  var orb=new T.Group(); scene.add(orb); orb.rotation.x=0.35;
  /* fibonacci sphere points */
  var N=98,pts=[],off=2/N,inc=Math.PI*(3-Math.sqrt(5));
  for(var i=0;i<N;i++){var y=i*off-1+off/2,r=Math.sqrt(1-y*y),phi=i*inc;
    pts.push(new T.Vector3(Math.cos(phi)*r,y,Math.sin(phi)*r));}
  var pg=new T.BufferGeometry().setFromPoints(pts);
  orb.add(new T.Points(pg,new T.PointsMaterial({color:0xC4B5FD,size:0.055,sizeAttenuation:true,transparent:true,opacity:0.95,blending:T.AdditiveBlending,depthWrite:false})));
  var segs=[],th=0.44;
  for(var a=0;a<N;a++){for(var b=a+1;b<N;b++){if(pts[a].distanceTo(pts[b])<th){segs.push(pts[a],pts[b]);}}}
  var lg=new T.BufferGeometry().setFromPoints(segs);
  orb.add(new T.LineSegments(lg,new T.LineBasicMaterial({color:0x8B7BFF,transparent:true,opacity:0.36,blending:T.AdditiveBlending,depthWrite:false})));
  orb.add(new T.Mesh(new T.SphereGeometry(0.55,24,24),new T.MeshBasicMaterial({color:0x6D7BFF,transparent:true,opacity:0.10,blending:T.AdditiveBlending,depthWrite:false})));
  /* pointer-drag rotation + momentum */
  var dragging=false,lx=0,ly=0,vx=0,vy=0;
  el.addEventListener('pointerdown',function(e){dragging=true;el.classList.add('dragging');lx=e.clientX;ly=e.clientY;vx=vy=0;el.setPointerCapture&&el.setPointerCapture(e.pointerId);});
  window.addEventListener('pointermove',function(e){if(!dragging)return;var dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;vy=dx*0.006;vx=dy*0.006;orb.rotation.y+=vy;orb.rotation.x+=vx;});
  window.addEventListener('pointerup',function(){dragging=false;el.classList.remove('dragging');});
  window.addEventListener('resize',function(){var s=el.clientWidth||150;renderer.setSize(s,s);});
  (function tick(){requestAnimationFrame(tick);
    if(!dragging){if(!reduce){orb.rotation.y+=0.0032;}orb.rotation.y+=vy;orb.rotation.x+=vx;vy*=0.94;vx*=0.94;}
    renderer.render(scene,camera);})();
})();
"""


def build(repo: Path, run_tests: bool = False, py_exe: str | None = None,
          shell_only: bool = False, web_out: "Path | None" = None) -> Path:
    # ``shell_only`` emits ONLY the public dashboard shell: the rebranded hero,
    # aggregate stats, and assets -- no per-paper cards, no papers/ detail pages,
    # and a status.json stripped of every per-paper title. public_sync.py uses it
    # to publish the harness UI to the PUBLIC repo without any paper content.
    # ``web_out`` writes the site somewhere other than <repo>/webapp (so building
    # the public shell never clobbers the private full site).
    web = Path(web_out) if web_out else repo / "webapp"
    if not shell_only:
        (web / "papers").mkdir(parents=True, exist_ok=True)
    (web / "assets").mkdir(parents=True, exist_ok=True)
    (web / "data").mkdir(parents=True, exist_ok=True)

    papers = discover(repo, run_tests, py_exe)

    (web / "assets" / "style.css").write_text(CSS, encoding="utf-8")
    (web / "assets" / "app.js").write_text(JS, encoding="utf-8")
    (web / "assets" / "hero-3d.js").write_text(HERO3D_JS, encoding="utf-8")

    # brand assets (favicon / emblem / social image) shipped from the committed source dir
    import shutil as _shutil
    _brand_src = Path(__file__).resolve().parent / "webapp_assets"
    for _fn in ("favicon.svg", "favicon.ico", "apple-touch-icon.png",
                "brand-emblem.png", "og-image.png"):
        _sp = _brand_src / _fn
        if _sp.exists():
            _shutil.copy(_sp, web / "assets" / _fn)

    cards = []
    if not shell_only:
        # stale per-paper pages cleanup
        current = {f"{p['code']}-{p['slug']}.html" for p in papers}
        for old in (web / "papers").glob("*.html"):
            if old.name not in current:
                old.unlink()
        for p in papers:
            page = render_paper(p, web)
            cards.append(render_card(p, web, page))

    counts = {c: sum(1 for p in papers if p["code"] == c) for c in AREAS}
    reproduced = sum(1 for p in papers if p["produced"])
    with_anim = sum(1 for p in papers if p["manim"])
    verdict_counts = {v: sum(1 for p in papers
                             if (p.get("verdict") or {}).get("verdict") == v)
                      for v in ("full", "partial", "minimal")}
    tests_passing = sum(1 for p in papers if p["tstat"].get("ran")
                        and p["tstat"]["failed"] == 0 and p["tstat"]["errors"] == 0
                        and p["tstat"]["passed"] > 0)
    # Honest hero stat: only claim "tests green" when a suite actually ran;
    # otherwise report how many tests were authored ("tests written").
    authored = sum(len(p["tests"]) for p in papers)
    ran_any = any(p["tstat"].get("ran") for p in papers)
    tests_stat = (f'<div class="stat"><b>{tests_passing}</b><span>tests green</span></div>'
                  if ran_any else
                  f'<div class="stat"><b>{authored}</b><span>tests written</span></div>')

    stat_cells = (
        f'<div class="stat"><b>{len(papers)}</b><span>papers</span></div>'
        f'<div class="stat"><b>{reproduced}</b><span>reproduced</span></div>'
        f'<div class="stat"><b>{with_anim}</b><span>animated</span></div>'
        + tests_stat +
        '<span class="stat-sep"></span>'
    )
    for c in AREAS:
        cls = "stat area empty" if counts[c] == 0 else "stat area"
        stat_cells += (f'<div class="{cls}" style="--c:{AREA_COLORS[c]}">'
                       f'<b>{counts[c]}</b><span>{c}</span></div>')

    # Area filter chips derived from live counts so ML/DL auto-light once
    # populated and chip colour stays in sync with AREA_COLORS.
    area_chips = ""
    for c in AREAS:
        cnt = counts[c]
        empty = cnt == 0
        acls = "chip chip-empty" if empty else "chip"
        extra = ' aria-disabled="true" title="no papers yet"' if empty else ""
        area_chips += (f'<button type="button" class="{acls}" data-group="area" '
                       f'data-area="{c}" aria-pressed="false" '
                       f'style="--c:{AREA_COLORS[c]}"{extra}>'
                       f'<span class="cdot"></span>{c}'
                       f'<span class="chip-n">{cnt}</span></button>')

    if shell_only:
        grid = (
            '<div class="empty-state"><h2>Public snapshot</h2>'
            '<p class="muted">This is the public code + dashboard snapshot of the harness. '
            'The per-paper reproductions (harvested PDFs, reproduced figures, tests, and Manim '
            'animations) live in the private corpus and are not redistributed here for copyright '
            'reasons. The counts above are the live totals from the full run.</p></div>')
    else:
        grid = "\n".join(cards) or (
            '<div class="empty-state"><h2>No papers yet</h2>'
            '<p class="muted">The daily pipeline populates <code>AI/ DS/ ML/ DL/</code> with '
            'reproduced papers. Re-run this generator after a harvest and each paper appears here '
            'with its original PDF, reproduced figures, tests, and Manim animation.</p></div>')

    legend = (
        '<div class="legend" aria-label="status legend">'
        '<span class="lg"><span class="dot ok"></span>reproduced</span>'
        '<span class="lg"><span class="dot warn"></span>pending</span>'
        '<span class="lg-sep"></span>'
        f'<span class="lg vk full"><span class="vchip ok">full</span>{verdict_counts["full"]}</span>'
        f'<span class="lg vk partial"><span class="vchip warn">partial</span>{verdict_counts["partial"]}</span>'
        f'<span class="lg vk minimal"><span class="vchip bad">minimal</span>{verdict_counts["minimal"]}</span>'
        '</div>')

    idx_html = INDEX.replace("@@FONTS@@", FONTS) \
                    .replace("@@STATS@@", stat_cells) \
                    .replace("@@AREACHIPS@@", area_chips) \
                    .replace("@@LEGEND@@", legend) \
                    .replace("@@CARDS@@", grid) \
                    .replace("@@UPDATED@@", datetime.now().strftime("%Y-%m-%d %H:%M")) \
                    .replace("@@COUNT@@", str(len(papers)))
    (web / "index.html").write_text(idx_html, encoding="utf-8")

    status = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "total": len(papers), "reproduced": reproduced, "animated": with_anim,
        "tests_passing": tests_passing, "by_area": counts,
        "by_verdict": verdict_counts,
        "papers": [] if shell_only else [{
            "id": f"{p['code']}-{p['slug']}", "code": p["code"], "title": p["title"],
            "produced": p["produced"], "status": p["status"],
            "verdict": (p.get("verdict") or {}).get("verdict", "minimal"),
            "verdict_score": (p.get("verdict") or {}).get("ratio", 0.0),
            "has_metrics": bool(p.get("metrics")),
            "has_paper_pdf": bool(p["orig_pdf"]), "has_summary_pdf": bool(p["summary_pdf"]),
            "original_figs": len(p["orig_figs"]), "reproduced_figs": len(p["repro_figs"]),
            "src_files": len(p["src"]), "tests": len(p["tests"]),
            "tests_ran": p["tstat"].get("ran", False),
            "tests_passed": p["tstat"].get("passed", 0),
            "tests_failed": p["tstat"].get("failed", 0),
            "has_manim": bool(p["manim"]), "has_original_data": bool(p["data_dir"]),
        } for p in papers],
    }
    (web / "data" / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

    print(f"[webapp] {len(papers)} papers · {reproduced} reproduced · {with_anim} animated "
          f"-> {web / 'index.html'}")
    return web / "index.html"


def serve(repo: Path, port: int) -> None:
    import http.server
    import functools
    import webbrowser
    import socketserver
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(repo))
    url = f"http://localhost:{port}/webapp/index.html"
    print(f"[serve] {url}  (Ctrl+C to stop)")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    with socketserver.TCPServer(("", port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] stopped")


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #
CSS = r""":root{
  --bg:#0A0E1A;--panel:#121829;--panel2:#0F1526;--ink:#E8ECF6;
  --muted:#8A93AD;--line:#232B42;--accent:#6D7BFF;--accent2:#34E0E0;
  --ok:#34D399;--warn:#FBBF24;--bad:#F87171;
  --r-sm:8px;--r-md:12px;--r-lg:16px;--radius:var(--r-lg);
  --grad:linear-gradient(135deg,#6D7BFF,#34E0E0);
  --serif:"Spectral",Georgia,serif;
  --sans:"Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
  --glass:rgba(18,24,41,.72);--glass2:rgba(15,21,38,.66);
  --shadow:0 18px 40px -20px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;font:15px/1.65 var(--sans);color:var(--ink);background:var(--bg);
  position:relative;min-height:100vh}
/* faint fixed dotted-grid + radial starfield texture */
.bg-grid,body::before{content:"";position:fixed;inset:0;z-index:-2;pointer-events:none;
  background-image:radial-gradient(rgba(138,147,173,.14) 1px,transparent 1.4px);
  background-size:26px 26px;opacity:.35;
  -webkit-mask-image:radial-gradient(120% 90% at 50% -5%,#000 30%,transparent 78%);
          mask-image:radial-gradient(120% 90% at 50% -5%,#000 30%,transparent 78%)}
body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(900px 520px at 82% -8%,rgba(109,123,255,.16),transparent 60%),
    radial-gradient(760px 460px at -8% 8%,rgba(52,224,224,.10),transparent 58%);}
a{color:inherit;text-decoration:none}
.muted{color:var(--muted)}.small{font-size:12.5px}
.mono{font-family:var(--mono)}
code{font-family:var(--mono);font-size:.85em;background:rgba(10,14,26,.7);border:1px solid var(--line);
  padding:1px 5px;border-radius:var(--r-sm)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
::selection{background:rgba(109,123,255,.4)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;
  vertical-align:middle;background:var(--muted)}
.dot.ok{background:var(--ok);box-shadow:0 0 8px rgba(52,211,153,.7)}
.dot.warn{background:var(--warn);box-shadow:0 0 8px rgba(251,191,36,.6)}
.dot.bad{background:var(--bad);box-shadow:0 0 8px rgba(248,113,113,.6)}

/* ---------- hero ---------- */
.hero{max-width:1280px;margin:0 auto;padding:64px 32px 26px;text-align:center;position:relative}
.hero .eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:3px;text-transform:uppercase;
  color:var(--muted);margin-bottom:18px}
.wordmark{font-family:"Orbitron",var(--sans);font-weight:700;font-size:clamp(30px,5.6vw,58px);
  line-height:1.08;letter-spacing:0;margin:0;
  background:linear-gradient(115deg,#6D7BFF,#34E0E0,#A78BFA,#6D7BFF);
  background-size:280% 280%;-webkit-background-clip:text;background-clip:text;color:transparent}
.hero-orb{position:relative;width:clamp(122px,18vw,162px);aspect-ratio:1;margin:2px auto 14px;cursor:grab;touch-action:none}
.hero-orb.dragging{cursor:grabbing}
.hero-orb canvas{display:block;width:100%;height:100%}
.tagline{font-family:var(--serif);font-style:italic;color:var(--muted);
  font-size:clamp(15px,2vw,20px);margin:16px 0 30px}
.stats{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;margin-top:22px}
.stat{background:var(--glass);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border:1px solid var(--line);border-radius:999px;padding:9px 18px;min-width:96px;
  display:flex;flex-direction:column;align-items:center;position:relative;overflow:hidden}
.stat b{font-family:var(--mono);font-size:22px;font-weight:600;line-height:1.1}
.stat span{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:1px;margin-top:2px}
.stat.area{border-color:color-mix(in srgb,var(--c) 45%,var(--line))}
.stat.area b{color:var(--c)}
.stat.area.empty{opacity:.5;border-color:var(--line)}
.stat.area.empty b{color:var(--muted)}
.stat-sep{width:1px;height:26px;background:var(--line);align-self:center;margin:0 4px}

.wrap{max-width:1280px;margin:0 auto;padding:8px 32px 72px}
/* sticky control bar (glass) */
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 26px;
  position:sticky;top:12px;z-index:8;padding:12px 14px;border:1px solid var(--line);
  border-radius:var(--radius);background:var(--glass);
  backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:var(--shadow)}
.controls input{flex:1;min-width:220px;background:var(--panel2);border:1px solid var(--line);
  color:var(--ink);padding:11px 15px;border-radius:var(--r-md);font:14px var(--sans);outline:none;
  transition:150ms ease}
.controls input::placeholder{color:var(--muted)}
.controls input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(109,123,255,.18)}
.chip{padding:8px 15px;border:1px solid var(--line);border-radius:999px;background:var(--panel2);
  cursor:pointer;font:inherit;font-size:13px;color:var(--muted);transition:150ms ease;user-select:none;
  display:inline-flex;align-items:center;gap:6px}
.chip:hover{color:var(--ink);border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.chip:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
.chip .cdot{width:8px;height:8px;border-radius:50%;background:var(--c,var(--muted))}
.chip .chip-n{font-family:var(--mono);font-size:11px;opacity:.85;margin-left:2px;padding-left:5px;
  border-left:1px solid color-mix(in srgb,var(--line) 70%,transparent)}
.chip.active{color:#fff;border-color:transparent;
  background:linear-gradient(135deg,rgba(109,123,255,.85),rgba(52,224,224,.7));
  box-shadow:0 4px 14px -4px rgba(109,123,255,.6)}
.chip-empty{opacity:.45;pointer-events:none}
.chipset{display:flex;gap:8px;flex-wrap:wrap}
.sep{width:1px;height:24px;background:var(--line);margin:0 4px}

/* ---------- cards ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(288px,100%),1fr));gap:20px}
.card{position:relative;background:var(--glass);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:var(--radius);
  overflow:hidden;transition:150ms ease;display:flex;flex-direction:column;
  box-shadow:0 10px 26px -18px rgba(0,0,0,.8)}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;
  background:var(--c);opacity:.9;z-index:2}
.card:hover,.card:focus-visible{transform:translateY(-5px);
  border-color:color-mix(in srgb,var(--c) 55%,var(--line));
  box-shadow:0 22px 44px -20px rgba(0,0,0,.85),0 0 0 1px color-mix(in srgb,var(--c) 30%,transparent)}
.card:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
.thumb-wrap{position:relative}
.card .thumb{width:100%;height:168px;object-fit:cover;background:var(--panel2);
  display:block;border-bottom:1px solid var(--line)}
.card .thumb.placeholder{display:flex;align-items:center;justify-content:center;
  background:
    radial-gradient(120% 120% at 20% 10%,color-mix(in srgb,var(--c) 26%,transparent),transparent 60%),
    linear-gradient(135deg,var(--panel),var(--panel2))}
.poster-glyph{font-size:62px;color:var(--c);opacity:.5;
  text-shadow:0 0 30px color-mix(in srgb,var(--c) 60%,transparent)}
.cbody{padding:15px 17px 17px;display:flex;flex-direction:column;gap:9px;flex:1}
.badge{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:11px;
  font-weight:600;letter-spacing:.5px;padding:4px 10px;border-radius:var(--r-sm);align-self:flex-start}
.badge.area{color:var(--c);background:color-mix(in srgb,var(--c) 15%,rgba(10,14,26,.6));
  border:1px solid color-mix(in srgb,var(--c) 45%,transparent)}
.thumb-wrap .badge{position:absolute;left:12px;top:12px;
  background:color-mix(in srgb,var(--c) 20%,rgba(10,14,26,.8));backdrop-filter:blur(6px)}
.card h3{font-family:var(--serif);margin:0;font-size:17.5px;line-height:1.3;font-weight:600;
  color:var(--ink)}
.meta{display:flex;gap:14px;color:var(--muted);font-size:12px;flex-wrap:wrap;margin-top:auto;
  font-family:var(--mono)}
.stat-dot{display:inline-flex;align-items:center}
.ticks{display:flex;gap:6px;flex-wrap:wrap}
.tick{font-family:var(--mono);font-size:10px;letter-spacing:.4px;text-transform:uppercase;
  color:var(--accent2);border:1px solid var(--line);border-radius:var(--r-sm);padding:1px 7px}
.empty-state{grid-column:1/-1;text-align:center;padding:80px 20px;background:var(--glass);
  border:1px dashed var(--line);border-radius:var(--radius)}
.empty-state h2{margin:0 0 10px;font-family:var(--serif)}
.no-results{grid-column:1/-1;text-align:center;padding:40px;color:var(--muted)}

/* ---------- paper page ---------- */
.paper{padding-bottom:60px}
header.phead{padding:22px 32px 0;max-width:1180px;margin:0 auto}
.back{color:var(--muted);font-size:13px;font-family:var(--mono)}
.back:hover{color:var(--accent2)}
.phead-band{margin-top:14px;padding:26px 28px;border-radius:var(--radius);
  border:1px solid var(--line);background:
    linear-gradient(180deg,color-mix(in srgb,var(--c) 12%,transparent),transparent 70%),
    var(--glass);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  border-left:3px solid var(--c);box-shadow:var(--shadow)}
header.phead h1{font-family:var(--serif);margin:14px 0 8px;font-size:clamp(26px,4vw,40px);
  line-height:1.15;font-weight:700;max-width:26ch;letter-spacing:-.3px}
.metaline{color:var(--muted);font-size:13.5px;font-family:var(--mono)}
.links{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.ext{font-family:var(--mono);font-size:12px;border:1px solid var(--line);border-radius:var(--r-sm);
  padding:5px 11px;color:var(--accent2);transition:150ms ease}
.ext:hover{background:rgba(52,224,224,.08);border-color:var(--accent2)}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.pill{font-family:var(--mono);font-size:12px;padding:5px 12px;border-radius:999px;
  border:1px solid var(--line);background:rgba(10,14,26,.5);display:inline-flex;align-items:center}
.pill.ok{border-color:rgba(52,211,153,.4);color:var(--ok);background:rgba(52,211,153,.08)}
.pill.warn{border-color:rgba(251,191,36,.4);color:var(--warn);background:rgba(251,191,36,.08)}

.tabs{display:flex;gap:6px;flex-wrap:wrap;max-width:1180px;margin:20px auto 0;padding:10px 32px;
  position:sticky;top:8px;z-index:8}
.tab{background:var(--panel2);border:1px solid var(--line);border-radius:999px;color:var(--muted);
  padding:8px 15px;font:13.5px var(--sans);cursor:pointer;transition:150ms ease;
  display:inline-flex;align-items:center;gap:6px}
.tab:hover{color:var(--ink);border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.tab.active{color:#fff;border-color:transparent;
  background:linear-gradient(135deg,rgba(109,123,255,.9),rgba(52,224,224,.7))}
.tabcount{font-family:var(--mono);font-size:11px;background:rgba(10,14,26,.5);
  border:1px solid var(--line);border-radius:999px;padding:0 7px;color:inherit}
.tab.active .tabcount{border-color:rgba(255,255,255,.3)}
.tab-empty{opacity:.5}

.pbody{max-width:1180px;margin:0 auto;padding:8px 32px}
.panel{display:none;background:var(--glass);backdrop-filter:blur(10px);
  -webkit-backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:var(--radius);
  padding:24px 28px;animation:fade .2s ease;box-shadow:var(--shadow)}
.panel.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.panel h1,.panel h2,.panel h3{font-family:var(--serif)}
.panel h3{margin:20px 0 10px;font-size:17px;position:relative;padding-bottom:6px;font-weight:600}
.panel h3::after{content:"";position:absolute;left:0;bottom:0;width:38px;height:2px;
  border-radius:999px;background:var(--grad)}
.panel h3:first-child{margin-top:0}
.panel p{color:#cdd4e6}
.panel em{font-style:italic;color:#dfe4f2}
.panel ol,.panel ul:not(.tree){color:#cdd4e6;padding-left:22px;margin:8px 0}
.panel ol li,.panel ul:not(.tree) li{margin:3px 0}
/* prose markdown tables (bare <table> from md_to_html; distinct from .mtab metrics) */
.panel>table,.panel table:not(.mtab){width:100%;border-collapse:collapse;margin:14px 0;font-size:13.5px;
  background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden}
.panel table:not(.mtab) th,.panel table:not(.mtab) td{padding:8px 12px;text-align:left;
  border-bottom:1px solid var(--line);color:#cdd4e6;vertical-align:top}
.panel table:not(.mtab) thead th{font-family:var(--mono);font-size:11px;letter-spacing:.5px;
  text-transform:uppercase;color:var(--muted);background:rgba(255,255,255,.02);font-weight:600}
.panel table:not(.mtab) tbody tr:last-child td{border-bottom:0}
.panel table:not(.mtab) tbody tr:hover td{background:rgba(255,255,255,.02)}
.panel hr{border:0;height:1px;margin:20px 0;background:linear-gradient(90deg,transparent,var(--line),transparent)}
.abstract{background:var(--panel2);border-left:3px solid var(--accent);border-radius:0 var(--r-md) var(--r-md) 0;
  padding:14px 18px;color:#cdd4e6;font-size:14px;margin-bottom:8px}

/* PDF */
.pdf-wrap{border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden;background:#1b2030}
.pdf-bar{display:flex;justify-content:space-between;align-items:center;padding:9px 13px;
  background:var(--panel2);border-bottom:1px solid var(--line)}
.pdf-bar .mono{color:var(--muted);font-size:12.5px}
.btn{font-family:var(--mono);font-size:12px;border:1px solid var(--line);border-radius:var(--r-sm);
  padding:5px 12px;color:var(--accent2);transition:150ms ease}
.btn:hover{background:rgba(52,224,224,.08);border-color:var(--accent2)}
.pdf{width:100%;height:82vh;border:0;background:#525659;display:block}

/* reproduction gallery */
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(240px,100%),1fr));gap:16px;margin-top:14px}
.gcell{margin:0;display:flex;flex-direction:column;gap:8px}
.gcell img{width:100%;aspect-ratio:16/10;object-fit:contain;border-radius:var(--r-md);background:#fff;
  border:1px solid var(--line);cursor:zoom-in;transition:150ms ease}
.gcell img:hover{transform:scale(1.015);box-shadow:0 12px 30px -14px rgba(0,0,0,.8)}
.gcell figcaption{color:var(--muted);font-size:12px}

/* figure comparison */
.figrow{display:grid;grid-template-columns:1fr 1px 1fr;gap:16px;margin:16px 0;align-items:start;
  padding:16px;background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-lg)}
.figrow::before{content:"";grid-column:2;align-self:stretch;
  background:linear-gradient(180deg,transparent,var(--line),transparent)}
.figcell{display:flex;flex-direction:column;gap:8px}
.figcell:nth-of-type(2){grid-column:3}
.figcell .tag{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:1px;
  color:var(--muted);border:1px solid var(--line);border-radius:var(--r-sm);padding:2px 8px;align-self:flex-start}
.figcell img{width:100%;border-radius:var(--r-md);background:#fff;border:1px solid var(--line);cursor:zoom-in;
  transition:150ms ease}
.figcell img:hover{transform:scale(1.01)}
.figcell figcaption{color:var(--muted);font-size:12px}
.figcell.empty .noimg{display:flex;align-items:center;justify-content:center;height:120px;
  color:var(--muted);border:1px dashed var(--line);border-radius:var(--r-md);font-style:italic}

/* tree */
ul.tree{list-style:none;margin:0;padding-left:16px;border-left:1px solid var(--line)}
.pbody>.panel ul.tree:first-child,.panel>ul.tree{padding-left:4px;border-left:0}
ul.tree li{margin:2px 0}
ul.tree details>summary{cursor:pointer;color:var(--accent2);font-family:var(--mono);font-size:13px;
  list-style:none;padding:1px 0}
ul.tree details>summary::before{content:"▸ ";color:var(--muted)}
ul.tree details[open]>summary::before{content:"▾ "}
ul.tree li.file a{font-family:var(--mono);font-size:13px;color:var(--ink)}
ul.tree li.file a:hover{color:var(--accent2);text-decoration:underline}
ul.tree li.file::before{content:"› ";color:var(--muted)}

/* tests */
.tbadge{display:inline-block;font-family:var(--mono);font-size:12.5px;font-weight:600;
  padding:7px 14px;border-radius:var(--r-md);border:1px solid var(--line);margin-bottom:12px}
.tbadge.pass{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08)}
.tbadge.fail{color:var(--bad);border-color:rgba(248,113,113,.4);background:rgba(248,113,113,.08)}
.tbadge.na{color:var(--muted)}
.filelist{list-style:none;padding:0;margin:8px 0;display:flex;flex-direction:column;gap:4px}
.filelist li{padding:2px 0;font-family:var(--mono);font-size:12.5px}
pre{background:var(--panel2);border:1px solid var(--line);border-radius:var(--r-md);padding:12px;overflow:auto;
  font-family:var(--mono)}
pre.small{font-size:12px}

/* ---------- cinematic animation player ---------- */
.cinema-wrap{display:flex;flex-direction:column;gap:16px}
.cinema-stage{display:flex;justify-content:center}
.cinema{margin:0;width:100%;max-width:860px;display:flex;flex-direction:column;gap:14px}
.cinema-frame{position:relative;border-radius:var(--r-lg);padding:10px;
  background:linear-gradient(180deg,var(--panel),var(--panel2));
  border:1px solid var(--line);
  box-shadow:var(--shadow)}
.anim-media{width:100%;border-radius:var(--r-md);display:block;background:#05070d}
.cinema-cap{text-align:center;color:var(--ink);font-family:var(--serif);font-style:italic;font-size:14.5px}
.filmstrip{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}
.film-cell{display:flex;align-items:center;gap:8px;background:var(--panel2);border:1px solid var(--line);
  border-radius:var(--r-md);padding:8px 12px;cursor:pointer;color:var(--muted);font-family:var(--mono);
  font-size:12px;transition:150ms ease}
.film-cell:hover{color:var(--ink);border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
.film-cell.active{color:#fff;border-color:transparent;
  background:linear-gradient(135deg,rgba(109,123,255,.85),rgba(52,224,224,.65))}
.film-idx{opacity:.7}
.cinema-stage.swap{animation:cswap .32s ease}
@keyframes cswap{from{opacity:0;transform:translateY(6px) scale(.995)}to{opacity:1;transform:none}}
.anim-empty{text-align:center;padding:44px 20px}
.anim-glyph{font-size:56px;color:var(--c);opacity:.5;margin-bottom:10px;
  text-shadow:0 0 34px color-mix(in srgb,var(--c) 60%,transparent)}

/* ---------- reproducibility analysis ---------- */
.verdict{display:flex;align-items:center;gap:20px;justify-content:space-between;
  padding:20px 22px;border-radius:var(--r-lg);border:1px solid var(--line);
  background:linear-gradient(180deg,color-mix(in srgb,var(--vc,var(--accent)) 12%,transparent),transparent 80%),var(--panel2);
  border-left:3px solid var(--vc,var(--accent))}
.verdict.ok{--vc:var(--ok)}.verdict.warn{--vc:var(--warn)}.verdict.bad{--vc:var(--bad)}
.verdict-main{display:flex;flex-direction:column;gap:5px}
.verdict-tag{font-family:var(--mono);font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--muted)}
.verdict-word{font-family:var(--serif);font-size:24px;font-weight:700;color:var(--vc,var(--ink))}
.verdict-blurb{color:#cdd4e6;font-size:13.5px;max-width:60ch}
.verdict-meter{position:relative;width:76px;height:76px;flex:none}
.verdict-meter .ring{width:76px;height:76px;transform:rotate(-90deg)}
.ring-bg{fill:none;stroke:var(--line);stroke-width:3}
.ring-fg{fill:none;stroke:var(--vc,var(--accent));stroke-width:3;stroke-linecap:round;
  transition:stroke-dasharray .6s ease}
.ring-num{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-family:var(--mono);font-size:19px;color:var(--ink)}
.ring-num small{font-size:11px;color:var(--muted);margin-left:1px}
.claims{display:flex;gap:8px;flex-wrap:wrap}
.claim{font-family:var(--mono);font-size:12px;padding:5px 11px;border-radius:var(--r-sm);
  border:1px solid var(--line);display:inline-flex;align-items:center;gap:4px}
.claim.y{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08)}
.claim.n{color:var(--bad);border-color:rgba(248,113,113,.4);background:rgba(248,113,113,.08)}
.siglist{list-style:none;padding:0;margin:8px 0 0;display:flex;flex-direction:column;gap:6px}
.sig{font-size:13.5px;color:#cdd4e6;font-family:var(--mono)}
.sig.ok{color:#bff0da}.sig.warn{color:#f6e2b0}.sig.bad{color:#f6bcbc}

/* metrics tables */
.metrics{margin-top:8px}
.mtab-wrap{overflow-x:auto;margin:6px 0 12px;border:1px solid var(--line);border-radius:var(--r-md)}
table.mtab{border-collapse:collapse;width:100%;font-family:var(--mono);font-size:12.5px}
table.mtab th,table.mtab td{padding:6px 11px;text-align:left;border-bottom:1px solid var(--line);
  white-space:nowrap}
table.mtab tr:last-child th,table.mtab tr:last-child td{border-bottom:0}
table.mtab th{color:var(--muted);font-weight:600}
table.mtab.matrix tr:first-child th{color:var(--accent2);position:sticky;top:0;
  background:var(--panel2)}
table.mtab.matrix th:first-child{color:var(--ink)}
table.mtab td{color:var(--ink)}
table.mtab tbody tr:hover,table.mtab tr:hover td{background:rgba(109,123,255,.05)}
.mnum{color:#dfe4f2}.mlist{color:var(--muted)}
.mbool.y{color:var(--ok)}.mbool.n{color:var(--bad)}
.mrow{display:flex;gap:10px;align-items:baseline;padding:3px 0;font-family:var(--mono);font-size:12.5px}
.mrow .mk{color:var(--muted);min-width:180px}
details.msec{border:1px solid var(--line);border-radius:var(--r-md);margin:8px 0;background:var(--panel2)}
details.msec>summary{cursor:pointer;padding:9px 13px;font-family:var(--mono);font-size:13px;
  color:var(--accent2);list-style:none}
details.msec>summary::before{content:"▸ ";color:var(--muted)}
details.msec[open]>summary::before{content:"▾ "}
details.msec .msec-body{padding:2px 13px 12px}

/* verdict chips (cards + legend) */
.vchip{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.5px;
  text-transform:uppercase;padding:2px 8px;border-radius:var(--r-sm);border:1px solid var(--line)}
.vchip.ok{color:var(--ok);border-color:rgba(52,211,153,.45);background:rgba(52,211,153,.1)}
.vchip.warn{color:var(--warn);border-color:rgba(251,191,36,.45);background:rgba(251,191,36,.1)}
.vchip.bad{color:var(--bad);border-color:rgba(248,113,113,.45);background:rgba(248,113,113,.1)}
.thumb-wrap .vchip{position:absolute;right:12px;top:12px;backdrop-filter:blur(6px);
  background:color-mix(in srgb,var(--bg) 82%,transparent)}
.pill.vpill{cursor:pointer}
.pill.vpill.ok{border-color:rgba(52,211,153,.4);color:var(--ok)}
.pill.vpill.warn{border-color:rgba(251,191,36,.4);color:var(--warn)}
.pill.vpill.bad{border-color:rgba(248,113,113,.4);color:var(--bad)}
.pill.vpill:hover{border-color:var(--accent2)}

/* dashboard legend */
.legend{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin:0 2px 18px;
  color:var(--muted);font-size:12.5px;font-family:var(--mono)}
.legend .lg{display:inline-flex;align-items:center;gap:6px}
.legend .lg.vk{gap:7px}
.legend .lg-sep{width:1px;height:16px;background:var(--line)}

/* figure captions */
.figcap,.gcell figcaption{display:flex;flex-direction:column;gap:3px}
.capline{color:#cdd4e6;font-size:12.5px;line-height:1.5}
.gcell figcaption .capline,.figcell .capline{font-family:var(--sans)}

/* lightbox */
.lightbox{display:none;position:fixed;inset:0;z-index:50;background:rgba(6,8,13,.94);
  align-items:center;justify-content:center;padding:30px;cursor:zoom-out;
  backdrop-filter:blur(6px);opacity:0;transition:opacity .18s ease}
.lightbox.open{display:flex;opacity:1}
.lb-fig{margin:0;display:flex;flex-direction:column;gap:12px;align-items:center;max-width:95vw}
.lightbox img{max-width:92vw;max-height:84vh;border-radius:var(--r-md);background:#fff;
  box-shadow:0 20px 60px rgba(0,0,0,.6);animation:pop .22s ease}
@keyframes pop{from{transform:scale(.97);opacity:.4}to{transform:none;opacity:1}}
.lb-cap{color:#e8ecf6;font-family:var(--sans);font-size:14px;max-width:82ch;text-align:center;
  line-height:1.55}
.lb-nav,.lb-close{position:fixed;background:var(--glass);border:1px solid var(--line);
  color:var(--ink);border-radius:999px;cursor:pointer;backdrop-filter:blur(8px);
  transition:150ms ease;z-index:51}
.lb-nav{top:50%;transform:translateY(-50%);width:52px;height:52px;font-size:30px;line-height:1}
.lb-nav.prev{left:22px}.lb-nav.next{right:22px}
.lb-nav:hover,.lb-close:hover{border-color:var(--accent2);color:var(--accent2)}
.lb-close{top:22px;right:22px;width:40px;height:40px;font-size:16px}

@media(max-width:720px){
  .figrow{grid-template-columns:1fr}
  .figrow::before,.figcell:nth-of-type(2){display:none}
  .figcell:nth-of-type(2){grid-column:1;display:flex}
  .hero,.wrap,.pbody,.tabs,header.phead{padding-left:18px;padding-right:18px}
  .pdf{height:70vh}
  .chip,.tab{min-height:44px;padding-top:11px;padding-bottom:11px}
  .ext,.btn,.back{min-height:44px;display:inline-flex;align-items:center}
  .lb-close{width:44px;height:44px}
  .controls,.tabs{position:static}
}
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;
    transition-duration:.001ms!important}
  html{scroll-behavior:auto}
}
"""

JS = r"""(function(){
  // ---- dashboard: search + area + status + verdict filters ----
  var q=document.getElementById('q');
  var cards=[].slice.call(document.querySelectorAll('.card'));
  if(q||cards.length){
    var area='ALL',status='ALL',verdict='ALL';
    var chips=[].slice.call(document.querySelectorAll('.chip'));
    var visible=cards.slice();
    function apply(){
      var t=(q&&q.value||'').trim().toLowerCase();
      var shown=0;visible=[];
      cards.forEach(function(c){
        var okA=area==='ALL'||c.dataset.code===area;
        var okS=status==='ALL'||c.dataset.status===status||
                (status==='animated'&&c.dataset.anim==='yes');
        var okV=verdict==='ALL'||c.dataset.verdict===verdict;
        var okT=!t||(c.dataset.search||'').indexOf(t)>-1;
        var vis=okA&&okS&&okV&&okT;c.style.display=vis?'':'none';
        if(vis){shown++;visible.push(c);}
      });
      var nr=document.getElementById('noresults');
      if(nr)nr.style.display=shown?'none':'block';
    }
    chips.forEach(function(ch){ch.addEventListener('click',function(){
      if(ch.getAttribute('aria-disabled')==='true')return;
      var group=ch.dataset.group||'area';
      chips.filter(function(x){return (x.dataset.group||'area')===group;})
           .forEach(function(x){x.classList.remove('active');x.setAttribute('aria-pressed','false');});
      ch.classList.add('active');ch.setAttribute('aria-pressed','true');
      if(group==='status')status=ch.dataset.status;
      else if(group==='verdict')verdict=ch.dataset.verdict;
      else area=ch.dataset.area;
      apply();
    });});
    if(q)q.addEventListener('input',apply);

    // keyboard: '/' focuses search; arrows move a roving focus across cards
    function focusCard(i){
      if(!visible.length)return;
      i=Math.max(0,Math.min(visible.length-1,i));
      visible[i].focus();
    }
    document.addEventListener('keydown',function(e){
      if(e.key==='/'&&q&&document.activeElement!==q){e.preventDefault();q.focus();return;}
      var ae=document.activeElement;
      var inSearch=ae===q;
      var onCard=ae&&ae.classList&&ae.classList.contains('card');
      if(!visible.length)return;
      if(inSearch){
        // only ArrowDown leaves the field; Left/Right/Up belong to the caret
        if(e.key==='ArrowDown'){e.preventDefault();focusCard(0);}
        return;
      }
      if((e.key==='ArrowDown'||e.key==='ArrowRight')&&onCard){
        e.preventDefault();
        focusCard(visible.indexOf(ae)+1);
      }else if((e.key==='ArrowUp'||e.key==='ArrowLeft')&&onCard){
        e.preventDefault();
        var idx=visible.indexOf(ae);
        if(idx<=0){if(q){q.focus();}}else{focusCard(idx-1);}
      }
    });
    // make cards keyboard-reachable
    cards.forEach(function(c){if(!c.hasAttribute('tabindex'))c.setAttribute('tabindex','0');});
    apply();
  }

  // ---- paper page: tabs ----
  var tabs=[].slice.call(document.querySelectorAll('.tab'));
  var panels=[].slice.call(document.querySelectorAll('.panel'));
  function activate(key){
    if(!tabs.some(function(t){return t.dataset.tab===key;}))return;
    // pause any clip when leaving the animation tab (stop decoding/looping)
    if(key!=='anim'){
      [].slice.call(document.querySelectorAll('.panel[data-panel="anim"] video'))
        .forEach(function(v){try{v.pause();}catch(_){}});
    }
    tabs.forEach(function(t){var on=t.dataset.tab===key;
      t.classList.toggle('active',on);t.setAttribute('aria-selected',on?'true':'false');});
    panels.forEach(function(p){p.classList.toggle('active',p.dataset.panel===key);});
    if(history.replaceState)history.replaceState(null,'','#'+key);
  }
  tabs.forEach(function(t){t.addEventListener('click',function(){activate(t.dataset.tab);});});
  // any element with data-goto (e.g. verdict pill) jumps to a tab
  [].slice.call(document.querySelectorAll('[data-goto]')).forEach(function(el){
    el.addEventListener('click',function(e){e.preventDefault();activate(el.dataset.goto);
      var top=document.querySelector('.tabs');if(top)top.scrollIntoView({behavior:'smooth',block:'start'});});
  });
  if(tabs.length){
    var h=(location.hash||'').replace('#','');
    if(h)activate(h);
    // [ and ] cycle tabs globally; Arrow/Home/End move selection when a tab is focused
    document.addEventListener('keydown',function(e){
      var lbOpen=document.querySelector('.lightbox.open');
      if(lbOpen)return;
      var act=tabs.filter(function(t){return t.classList.contains('active');})[0];
      var i=tabs.indexOf(act);
      if(e.key==='['||e.key===']'){
        var animOn=document.querySelector('.panel[data-panel="anim"].active');
        if(animOn)return; // anim tab owns [ ] would clash with clips; keep old guard
        if(i<0)return;
        activate(tabs[(i+(e.key===']'?1:tabs.length-1))%tabs.length].dataset.tab);
        return;
      }
      var ae=document.activeElement;
      var onTab=ae&&ae.classList&&ae.classList.contains('tab');
      if(!onTab)return; // arrows only steer tabs while a tab has focus
      var j=tabs.indexOf(ae),n=tabs.length,tgt=-1;
      if(e.key==='ArrowRight'||e.key==='ArrowDown')tgt=(j+1)%n;
      else if(e.key==='ArrowLeft'||e.key==='ArrowUp')tgt=(j-1+n)%n;
      else if(e.key==='Home')tgt=0;
      else if(e.key==='End')tgt=n-1;
      if(tgt>=0){e.preventDefault();activate(tabs[tgt].dataset.tab);tabs[tgt].focus();}
    });
  }

  // ---- cinematic player: filmstrip switching + keyboard ----
  var stage=document.getElementById('cinema-stage');
  var cells=[].slice.call(document.querySelectorAll('.film-cell'));
  var reduceMotion=!!(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  function showClip(cell){
    cells.forEach(function(c){c.classList.remove('active');});
    cell.classList.add('active');
    var src=cell.dataset.src,name=cell.dataset.name,area=cell.dataset.area,
        lead=cell.dataset.lead||'Reproduced finding';
    var autoplay=reduceMotion?'':' autoplay';
    var media=cell.dataset.gif==='1'
      ? '<img class="anim-media" src="'+src+'" alt="'+name+'" loading="lazy">'
      : '<video class="anim-media" controls loop muted playsinline preload="metadata"'+autoplay+'>'+
        '<source src="'+src+'">Your browser cannot play this video.</video>';
    stage.classList.remove('swap');void stage.offsetWidth;stage.classList.add('swap');
    stage.innerHTML='<figure class="cinema"><div class="cinema-frame">'+media+
      '</div><figcaption class="cinema-cap">'+lead+' — '+area+
      '<span class="mono muted"> · '+name+'</span></figcaption></figure>';
  }
  if(stage&&cells.length){
    cells.forEach(function(cell){cell.addEventListener('click',function(){showClip(cell);});});
    document.addEventListener('keydown',function(e){
      var animOn=document.querySelector('.panel[data-panel="anim"].active');
      if(!animOn)return;
      var vid=stage.querySelector('video');
      if(e.key===' '&&vid){
        var ae=document.activeElement;
        // only toggle when focus isn't on an interactive control (tab/link/cell)
        if(!ae||!/^(A|BUTTON|INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)){
          e.preventDefault();vid.paused?vid.play():vid.pause();
        }
        return;
      }
      if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;
      var af=document.activeElement;
      if(af&&af.classList&&af.classList.contains('tab'))return; // tabs own arrows when focused
      e.preventDefault();
      var i=cells.map(function(c){return c.classList.contains('active');}).indexOf(true);
      if(i<0)i=0;
      showClip(cells[(i+(e.key==='ArrowRight'?1:cells.length-1))%cells.length]);
    });
  }

  // ---- figure lightbox: gallery nav + captions ----
  var lb=document.getElementById('lightbox'),lbimg=document.getElementById('lbimg'),
      lbcap=document.getElementById('lbcap');
  if(lb&&lbimg){
    var zoomers=[],cur=0,lastFocus=null;
    var pv=document.getElementById('lbprev'),nx=document.getElementById('lbnext'),
        cl=document.getElementById('lbclose');
    function refresh(){
      zoomers=[].slice.call(document.querySelectorAll('.zoom')).filter(function(z){
        return z.offsetParent!==null; // only visible (active panel)
      });
    }
    function paint(){
      var z=zoomers[cur];if(!z)return;
      lbimg.src=z.dataset.full||z.src;
      var cap=z.dataset.cap||'';
      lbimg.alt=cap||z.alt||'zoomed figure';
      if(lbcap){lbcap.textContent=cap;lbcap.style.display=cap?'':'none';}
    }
    function open(z){refresh();cur=Math.max(0,zoomers.indexOf(z));paint();
      lastFocus=document.activeElement;
      lb.classList.add('open');lb.setAttribute('aria-hidden','false');
      document.body.style.overflow='hidden';
      if(cl)cl.focus();}
    function close(){lb.classList.remove('open');lb.setAttribute('aria-hidden','true');
      document.body.style.overflow='';
      if(lastFocus&&lastFocus.focus)lastFocus.focus();lastFocus=null;}
    function step(d){if(!zoomers.length)return;cur=(cur+d+zoomers.length)%zoomers.length;paint();}
    // click or keyboard (Enter/Space) on a figure opens the viewer
    document.addEventListener('click',function(e){
      var el=e.target;
      if(el.classList&&el.classList.contains('zoom')){e.preventDefault();open(el);}
    });
    document.addEventListener('keydown',function(e){
      var el=e.target;
      if(el&&el.classList&&el.classList.contains('zoom')&&(e.key==='Enter'||e.key===' ')){
        e.preventDefault();open(el);
      }
    });
    if(pv)pv.addEventListener('click',function(e){e.stopPropagation();step(-1);});
    if(nx)nx.addEventListener('click',function(e){e.stopPropagation();step(1);});
    if(cl)cl.addEventListener('click',function(e){e.stopPropagation();close();});
    lb.addEventListener('click',function(e){
      if(e.target===lb||e.target===lbimg||(e.target.classList&&e.target.classList.contains('lb-fig')))close();
    });
    document.addEventListener('keydown',function(e){
      if(!lb.classList.contains('open'))return;
      if(e.key==='Escape'){close();return;}
      if(e.key==='ArrowRight'){e.preventDefault();step(1);return;}
      if(e.key==='ArrowLeft'){e.preventDefault();step(-1);return;}
      if(e.key==='Tab'){
        // trap Tab within the lightbox controls (close / prev / next)
        var f=[cl,pv,nx].filter(function(b){return b;});
        if(!f.length)return;
        e.preventDefault();
        var idx=f.indexOf(document.activeElement);
        idx=e.shiftKey?(idx<=0?f.length-1:idx-1):(idx>=f.length-1?0:idx+1);
        f[idx].focus();
      }
    });
  }
})();
"""

INDEX = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Agentic AI Researcher — Computational Observatory</title>
<link rel="icon" type="image/svg+xml" href="assets/favicon.svg?v=3">
<link rel="icon" href="assets/favicon.ico?v=3" sizes="any">
<link rel="apple-touch-icon" href="assets/apple-touch-icon.png?v=3">
<meta property="og:type" content="website">
<meta property="og:title" content="Agentic AI Researcher — Computational Observatory">
<meta property="og:description" content="Autonomous paper reproduction — reproduced results, honestly evaluated.">
<meta property="og:image" content="assets/og-image.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="assets/og-image.png">
@@FONTS@@
<link rel=stylesheet href="assets/style.css"></head><body>
<div class="bg-grid" aria-hidden="true"></div>
<header class=hero>
  <div class=eyebrow>Computational Observatory</div>
  <div class="hero-orb" id="hero-orb" aria-hidden="true"><noscript><img src="assets/brand-emblem.png" alt="" width=110 height=110 style="border-radius:16px"></noscript></div>
  <h1 class=wordmark>Agentic AI Researcher</h1>
  <div class=tagline>Autonomous paper reproduction</div>
  <div class=stats>@@STATS@@</div>
</header>
<div class=wrap>
  <div class=controls>
    <input id=q aria-label="Search papers by title, area, or venue" placeholder="Search @@COUNT@@ papers by title, area, venue…   ( press / )">
    <div class=chipset>
      <button type=button class="chip active" aria-pressed=true data-group=area data-area=ALL>All</button>
      @@AREACHIPS@@
    </div>
    <span class=sep></span>
    <div class=chipset>
      <button type=button class="chip active" aria-pressed=true data-group=status data-status=ALL>Any status</button>
      <button type=button class=chip aria-pressed=false data-group=status data-status=reproduced>Reproduced</button>
      <button type=button class=chip aria-pressed=false data-group=status data-status=animated>Animated</button>
    </div>
    <span class=sep></span>
    <div class=chipset>
      <button type=button class="chip active" aria-pressed=true data-group=verdict data-verdict=ALL>Any verdict</button>
      <button type=button class=chip aria-pressed=false data-group=verdict data-verdict=full>Full</button>
      <button type=button class=chip aria-pressed=false data-group=verdict data-verdict=partial>Partial</button>
      <button type=button class=chip aria-pressed=false data-group=verdict data-verdict=minimal>Minimal</button>
    </div>
  </div>
  @@LEGEND@@
  <div class=grid>@@CARDS@@
    <div id=noresults class=no-results role=status aria-live=polite style="display:none">No papers match your filters.</div>
  </div>
</div>
<script src="assets/app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"></script>
<script src="assets/hero-3d.js"></script></body></html>"""


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Build the static reproduction web app.")
    ap.add_argument("--repo", type=Path, default=here.parent)
    ap.add_argument("--run-tests", action="store_true",
                    help="execute each paper's pytest suite and show a pass/fail badge")
    ap.add_argument("--python", type=str, default=None,
                    help="python.exe used for --run-tests (default: the venv or this interpreter)")
    ap.add_argument("--serve", action="store_true", help="serve the site over HTTP and open a browser")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--shell-only", action="store_true",
                    help="emit only the public dashboard shell (no per-paper cards/pages or titles)")
    ap.add_argument("--web-out", type=Path, default=None,
                    help="write the webapp to this dir instead of <repo>/webapp (used by public_sync)")
    args = ap.parse_args()

    repo = args.repo.resolve()
    py_exe = args.python
    if args.run_tests and not py_exe:
        cand = repo / ".venv" / "Scripts" / "python.exe"
        py_exe = str(cand) if cand.exists() else sys.executable

    build(repo, run_tests=args.run_tests, py_exe=py_exe,
          shell_only=args.shell_only, web_out=args.web_out)
    if args.serve:
        serve(repo, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
