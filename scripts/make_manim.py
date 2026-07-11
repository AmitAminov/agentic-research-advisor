#!/usr/bin/env python3
"""
Per-paper animation generator using 3Blue1Brown's Manim (Community edition).

Given a reproduced paper folder (as produced by ``reproduce.py`` --
``<repo>/<AREA>/<slug>/`` containing paper.md, REPRODUCTION.md,
reproduced_results/, results/), this module derives the paper's *key finding*
**and its reproduced data**, then renders a short, self-contained Manim
``Scene`` that VISUALISES it as a real scientific animation in the 3b1b style:
animated Axes with plotted line / scatter / bar data driven by the paper's own
numbers, a geometric transformation flourish, and branded title / outro cards.
The output lands at::

    <paper>/manim/<name>.mp4     (preferred)
    <paper>/manim/<name>.gif     (fallback, if ffmpeg present but mp4 fails)
    <paper>/manim/<name>.png     (fallback, still frame -- needs no ffmpeg)

Design goals (per project constraints):
  * Genuinely graphical: the generated scene builds Axes / Dots / Lines /
    Polygons / plots / Transforms from the reproduced numbers -- not just text.
  * Data-driven: reads ``reproduced_results/metrics.json`` (and ``results/`` +
    any CSV series), auto-detects the best chart (scatter of reproduced-vs-paper
    agreement, grouped bars, line series, or single bars) and falls back to an
    elegant animated conceptual diagram when there is no numeric series.
  * Reuses the branded primitive library ``scripts/manim_scientific.py`` so the
    look stays consistent and the scene stays short.
  * Pango ``Text`` only -- NO LaTeX -- so it renders on any machine.
  * Never break the pipeline: import failures, render failures, missing ffmpeg,
    and any bad/missing data are all caught and recorded gracefully. Any single
    stage that fails is skipped; a paper without usable data still gets a clean
    title -> conceptual diagram -> outro clip.
  * If Manim import fails (e.g. install still finishing), we STILL write the
    scene script and note it, so a later run can render it.

Public API:
    render_for_paper(paper_dir) -> dict   # the record other tools consume

CLI:
    python make_manim.py --smoke            # minimal example scene smoke test
    python make_manim.py --paper <dir>      # render one paper folder
    python make_manim.py --backfill         # every reproduced paper in the repo

This module deliberately does NOT modify config.json (it only reads it) and
never touches anything under a ``raw/`` folder.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

# Name of the Scene class inside the generated script (kept stable so the
# render command and any downstream tooling agree on it).
SCENE_CLASS = "FindingScene"
DEFAULT_OUTPUT_NAME = "finding"

# Render frame rate. Medium quality is nominally 720p30, but these scenes are
# now richer (extra reveals + camera moves) and this pipeline runs UNATTENDED
# under a fixed per-render timeout on CPU-only hardware. Rendering at 20fps
# keeps the full 720p crispness while cutting the frame count ~1/3 so a heavier
# multi-series scene still finishes comfortably inside the timeout (otherwise it
# would silently degrade to a PNG still). Motion still reads smoothly.
RENDER_FPS = 20

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
CONFIG_PATH = REPO_ROOT / "config.json"

# Ensure the venv's Scripts dir (where ffmpeg.exe is placed for this project) is
# on PATH so shutil.which("ffmpeg") + the manim subprocess can find it and emit
# real MP4/GIF video instead of degrading to a still PNG frame.
_scripts_dir = os.path.dirname(sys.executable)
if _scripts_dir and _scripts_dir not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _scripts_dir + os.pathsep + os.environ.get("PATH", "")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    """Read a text file, tolerating encoding issues; '' if missing."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _first_heading(md: str) -> str | None:
    """Return the first Markdown heading / title-ish line, if any."""
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            return s.lstrip("#").strip() or None
        m = re.match(r"^\*\*(.+?)\*\*\s*$", s)
        if m:
            return m.group(1).strip()
        return s[:200]
    return None


def _clean(text: str, limit: int = 240) -> str:
    """Collapse whitespace / strip markdown noise and truncate."""
    text = re.sub(r"[`*_>#\[\]]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _pretty_title(slug_or_title: str) -> str:
    t = slug_or_title.replace("-", " ").replace("_", " ").strip()
    return _clean(t, 120) or "Research finding"


def _pretty_label(s: str, limit: int = 22) -> str:
    s = str(s).replace("_", " ").strip()
    return _clean(s, limit)


def _paragraph_after(md: str, keywords: tuple[str, ...]) -> str | None:
    """Find the first non-empty paragraph at/after a heading matching a keyword."""
    lines = md.splitlines()
    low = [ln.lower() for ln in lines]
    for i, ln in enumerate(low):
        if ln.strip().startswith("#") and any(k in ln for k in keywords):
            buf: list[str] = []
            for j in range(i + 1, len(lines)):
                s = lines[j].strip()
                if s.startswith("#"):
                    break
                if not s and buf:
                    break
                if s:
                    buf.append(s)
            if buf:
                return " ".join(buf)
    return None


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _finite(seq: Any) -> list[float]:
    out: list[float] = []
    for v in seq or []:
        if _is_num(v):
            out.append(float(v))
    return out


def _nonconstant(seq: list[float]) -> bool:
    if len(seq) < 2:
        return False
    lo, hi = min(seq), max(seq)
    scale = max(abs(lo), abs(hi), 1e-12)
    return (hi - lo) > 0.01 * scale


# ---------------------------------------------------------------------------
# Data-source loading
# ---------------------------------------------------------------------------

def _load_json_sources(paper_dir: Path) -> list[tuple[str, Any]]:
    """Return (name, parsed-json) for each usable metrics/results JSON file."""
    out: list[tuple[str, Any]] = []
    candidates = [
        paper_dir / "reproduced_results" / "metrics.json",
        paper_dir / "reproduced_results" / "results.json",
        paper_dir / "results" / "results.json",
        paper_dir / "results" / "metrics.json",
    ]
    for c in candidates:
        raw = _read_text(c)
        if not raw.strip():
            continue
        try:
            out.append((c.name, json.loads(raw)))
        except Exception:
            continue
    return out


def _load_csv_series(paper_dir: Path) -> list[dict[str, Any]]:
    """Parse numeric columns from any CSV under reproduced_results/ or results/."""
    series: list[dict[str, Any]] = []
    dirs = [paper_dir / "reproduced_results", paper_dir / "results"]
    for d in dirs:
        if not d.is_dir():
            continue
        for csv_path in sorted(d.glob("*.csv")):
            try:
                cols = _read_csv_columns(csv_path)
            except Exception:
                continue
            if not cols:
                continue
            names = list(cols.keys())
            # choose an x column (first monotonic-ish numeric column)
            xname = names[0]
            xs = cols[xname]
            for yname in names[1:]:
                ys = cols[yname]
                n = min(len(xs), len(ys))
                if n >= 3 and _nonconstant(ys[:n]):
                    series.append({"name": _pretty_label(yname, 18),
                                   "xs": xs[:n], "ys": ys[:n]})
            if series:
                break
        if series:
            break
    return series[:3]


def _read_csv_columns(csv_path: Path) -> dict[str, list[float]]:
    """Return {header: [floats]} for numeric columns (skips non-numeric)."""
    text = _read_text(csv_path)
    if not text.strip():
        return {}
    reader = csv.reader(text.splitlines())
    rows = [r for r in reader if r]
    if len(rows) < 4:
        return {}
    header = [h.strip() for h in rows[0]]
    # detect whether row 0 is a header (non-numeric) or data
    def _row_numeric(r):
        return all(_looks_numeric(x) for x in r)
    if _row_numeric(rows[0]):
        header = [f"col{i}" for i in range(len(rows[0]))]
        data_rows = rows
    else:
        data_rows = rows[1:]
    cols: dict[str, list[float]] = {h: [] for h in header}
    for r in data_rows:
        if len(r) != len(header):
            continue
        for h, cell in zip(header, r):
            try:
                cols[h].append(float(cell))
            except Exception:
                cols[h].append(math.nan)
    # keep columns that are mostly numeric
    good: dict[str, list[float]] = {}
    for h, vals in cols.items():
        finite = [v for v in vals if math.isfinite(v)]
        if len(finite) >= max(3, int(0.6 * len(vals))):
            good[h] = [v if math.isfinite(v) else 0.0 for v in vals]
    return good


def _looks_numeric(x: str) -> bool:
    try:
        float(x)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Numeric-structure detectors  ->  a "viz" spec
# ---------------------------------------------------------------------------

PAPER_MARKERS = {"paper", "paper_target", "paper_reference", "reference",
                 "published", "original", "ground_truth", "target", "true"}
REPRO_MARKERS = {"reproduced", "repro", "ours", "our", "reproduction", "mine"}
NOISE_COMPONENTS = {"config", "params", "param", "seeds", "seed", "provenance",
                    "std", "per_seed", "runtime_sec", "note", "figures"}


def _flatten(obj: Any, prefix: tuple = ()):
    """Yield (path_tuple, float_value) for every finite numeric leaf."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, prefix + (str(k),))
    elif isinstance(obj, (list, tuple)):
        return  # arrays handled by the series detector, not here
    elif _is_num(obj):
        yield prefix, float(obj)


def _canon(path: tuple) -> tuple[tuple, str]:
    """Strip side-markers from a path; return (base_path, side)."""
    base: list[str] = []
    side = "neutral"
    for c in path:
        cl = c.lower()
        if cl in PAPER_MARKERS:
            side = "paper"
            continue
        if cl in REPRO_MARKERS:
            if side != "paper":
                side = "repro"
            continue
        base.append(cl)
    return tuple(base), side


def _detect_pairs(root: Any) -> list[tuple[str, float, float]]:
    """
    Find (label, paper_value, reproduced_value) triples by matching leaves whose
    canonical path agrees but which sit on the paper vs the reproduced side.
    A 'neutral' leaf (no marker) is treated as the reproduced side when a paper
    counterpart exists.
    """
    groups: dict[tuple, dict[str, float]] = {}
    for path, val in _flatten(root):
        if any(c.lower() in NOISE_COMPONENTS for c in path):
            continue
        base, side = _canon(path)
        if not base:
            continue
        groups.setdefault(base, {})[side] = val
    pairs: list[tuple[str, float, float]] = []
    for base, g in groups.items():
        p = g.get("paper")
        r = g.get("repro", g.get("neutral"))
        if p is None or r is None:
            continue
        label = " ".join(base[-2:]) if len(base) >= 2 else base[-1]
        pairs.append((_pretty_label(label, 20), p, r))
    return pairs


def _detect_prefix_families(root: Any) -> dict[str, Any] | None:
    """
    Detect two parallel families of numeric keys that share suffixes, e.g.
    ``active_regret`` / ``projection_regret`` -> grouped bars with the two
    prefixes as series and the shared suffixes as labels.
    """
    fam: dict[str, dict[str, float]] = {}
    for path, val in _flatten(root):
        if any(c.lower() in NOISE_COMPONENTS for c in path):
            continue
        key = path[-1]
        if "_" not in key:
            continue
        prefix, suffix = key.split("_", 1)
        fam.setdefault(prefix, {})[suffix] = val
    if len(fam) < 2:
        return None
    # score every prefix pair by number of shared suffixes
    best = None
    prefixes = list(fam.keys())
    for i in range(len(prefixes)):
        for j in range(i + 1, len(prefixes)):
            a, b = prefixes[i], prefixes[j]
            shared = [s for s in fam[a] if s in fam[b]]
            if len(shared) >= 3 and (best is None or len(shared) > len(best[2])):
                best = (a, b, shared)
    if not best:
        return None
    a, b, shared = best
    shared = shared[:5]
    return {
        "kind": "grouped_bars",
        "labels": [_pretty_label(s, 16) for s in shared],
        "paper": [fam[a][s] for s in shared],
        "repro": [fam[b][s] for s in shared],
        "paper_name": _pretty_label(a, 16),
        "repro_name": _pretty_label(b, 16),
        "normalize": "group",
    }


def _detect_series(root: Any) -> list[dict[str, Any]]:
    """Find named numeric arrays (>=3 pts, non-constant) as line series."""
    found: list[dict[str, Any]] = []

    def rec(node: Any):
        if isinstance(node, dict):
            arrays = {k: _finite(v) for k, v in node.items()
                      if isinstance(v, (list, tuple)) and len(_finite(v)) >= 3}
            if arrays:
                xkey = None
                for cand in ("x", "xs", "lambda", "lambdas", "steps", "epoch",
                             "epochs", "iter", "iters", "iteration", "t",
                             "time", "grid", "u", "n"):
                    for k in arrays:
                        if k.lower() == cand or cand in k.lower():
                            xkey = k
                            break
                    if xkey:
                        break
                xarr = arrays.get(xkey) if xkey else None
                for yk, ys in arrays.items():
                    if yk == xkey:
                        continue
                    if not _nonconstant(ys):
                        continue
                    if xarr and len(xarr) == len(ys):
                        xs = xarr
                    else:
                        xs = list(range(len(ys)))
                    found.append({"name": _pretty_label(yk, 16),
                                  "xs": xs, "ys": ys})
            for v in node.values():
                rec(v)

    rec(root)
    return found[:3]


def _detect_flat_metrics(root: Any) -> dict[str, Any] | None:
    """Collect comparable scalar metrics for a single-series bar chart."""
    value_keyed: list[tuple[str, float]] = []
    top_level: list[tuple[str, float]] = []
    for path, val in _flatten(root):
        if any(c.lower() in NOISE_COMPONENTS for c in path):
            continue
        key = path[-1].lower()
        if key in {"mean", "value", "score", "acc", "accuracy", "success",
                   "reward", "rate", "success_rate"} and len(path) >= 2:
            value_keyed.append((_pretty_label(path[-2], 16), val))
        elif len(path) <= 2:
            top_level.append((_pretty_label(path[-1], 16), val))

    chosen = value_keyed if len(value_keyed) >= 2 else top_level
    # dedupe by label, keep order
    seen: set[str] = set()
    uniq: list[tuple[str, float]] = []
    for lab, v in chosen:
        if lab in seen:
            continue
        seen.add(lab)
        uniq.append((lab, v))
    if len(uniq) < 2:
        return None
    # if scales span wildly, keep the largest same-scale cluster around median
    vals = sorted(abs(v) for _, v in uniq if v != 0)
    if vals and vals[-1] / (vals[len(vals) // 2] or 1e-9) > 200:
        med = vals[len(vals) // 2]
        uniq = [(lbl, v) for lbl, v in uniq
                if med / 200 <= abs(v) <= med * 200 or v == 0]
    uniq = uniq[:6]
    if len(uniq) < 2:
        return None
    return {"kind": "bars",
            "labels": [lbl for lbl, _ in uniq],
            "values": [v for _, v in uniq]}


def _extract_viz(paper_dir: Path, finding: dict[str, Any]) -> dict[str, Any]:
    """
    Inspect the paper's reproduced numeric data and return a ``viz`` spec the
    scene can render. Priority: reproduced-vs-paper scatter -> paired grouped
    bars -> parallel-family grouped bars -> line series -> single bars ->
    conceptual diagram. Always returns *something*.
    """
    sources = _load_json_sources(paper_dir)

    # 1) reproduced-vs-paper pairs (scatter if many, grouped bars if a few)
    for name, obj in sources:
        pairs = _detect_pairs(obj)
        pairs = [(lbl, p, r) for (lbl, p, r) in pairs
                 if math.isfinite(p) and math.isfinite(r)]
        if len(pairs) >= 6:
            pairs = pairs[:80]
            return {"kind": "scatter",
                    "xs": [p for _, p, _ in pairs],
                    "ys": [r for _, _, r in pairs],
                    "x_label": "Paper value",
                    "y_label": "Reproduced value",
                    "diagonal": True,
                    "caption": "Each point compares one reproduced metric to the "
                               "paper's reported value.",
                    "source": name}
        if 2 <= len(pairs) <= 5:
            return {"kind": "grouped_bars",
                    "labels": [lbl for lbl, _, _ in pairs],
                    "paper": [p for _, p, _ in pairs],
                    "repro": [r for _, _, r in pairs],
                    "paper_name": "Paper", "repro_name": "Reproduced",
                    "normalize": "group",
                    "source": name}

    # 2) parallel prefix families -> grouped bars (e.g. active vs projection)
    for name, obj in sources:
        fam = _detect_prefix_families(obj)
        if fam:
            fam["source"] = name
            return fam

    # 3) numeric line series (JSON arrays, then CSV columns)
    for name, obj in sources:
        series = _detect_series(obj)
        if series:
            return {"kind": "line", "series": series,
                    "x_label": "", "y_label": "", "source": name}
    csv_series = _load_csv_series(paper_dir)
    if csv_series:
        return {"kind": "line", "series": csv_series,
                "x_label": "", "y_label": "", "source": "csv"}

    # 4) flat scalar metrics -> single bars
    for name, obj in sources:
        bars = _detect_flat_metrics(obj)
        if bars:
            bars["source"] = name
            return bars

    # 5) conceptual diagram fallback (still graphical)
    nodes = _concept_nodes(finding)
    return {"kind": "concept", "nodes": nodes,
            "title": "Reproduction pipeline"}


def _concept_nodes(finding: dict[str, Any]) -> list[str]:
    """Derive 3-4 short pipeline node labels for the conceptual fallback."""
    area = (finding.get("area") or "").upper()
    base = {"AI": ["Agent", "Policy", "Reward", "Result"],
            "DS": ["Data", "Model", "Estimate", "Result"],
            "ML": ["Input", "Train", "Predict", "Result"],
            "DL": ["Input", "Network", "Loss", "Result"]}.get(
                area, ["Paper", "Reproduce", "Compare", "Result"])
    return base


# ---------------------------------------------------------------------------
# Finding extraction (title + one-line finding + viz spec)
# ---------------------------------------------------------------------------

def _extract_finding(paper_dir: Path) -> dict[str, Any]:
    """Derive a compact, animation-friendly description of the key finding."""
    title = _pretty_title(paper_dir.name)
    finding = ""
    provenance: list[str] = []

    paper_md = _read_text(paper_dir / "paper.md")
    if paper_md:
        h = _first_heading(paper_md)
        if h:
            title = _clean(h, 120)
            provenance.append("paper.md")

    summary = _read_text(paper_dir / "summary.md")
    if summary.strip():
        finding = _clean(_paragraph_after(summary, ("finding", "summary", "claim")) or summary, 200)
        provenance.append("summary.md")

    repro = _read_text(paper_dir / "REPRODUCTION.md")
    if not finding and repro.strip():
        claim = _paragraph_after(repro, ("central claim", "claim", "reproduced", "summary"))
        finding = _clean(claim or repro, 200)
        provenance.append("REPRODUCTION.md")

    if not finding and paper_md:
        finding = _clean(_paragraph_after(paper_md, ("abstract",)) or paper_md, 200)
        if "paper.md" not in provenance:
            provenance.append("paper.md")

    if not finding:
        finding = "Key research finding reproduced under CPU-only, scaled-down constraints."

    area = ""
    parent = paper_dir.parent.name
    if parent in {"AI", "DL", "DS", "ML"}:
        area = parent

    result: dict[str, Any] = {
        "title": title,
        "subtitle": "Reproduced finding" if repro.strip() else "Key finding",
        "finding": finding,
        "area": area,
        "slug": paper_dir.name,
        "provenance": provenance,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    # attach the data-driven visualisation spec (never raises)
    try:
        viz = _extract_viz(paper_dir, result)
    except Exception as exc:  # noqa: BLE001
        viz = {"kind": "concept", "nodes": _concept_nodes(result),
               "title": "Reproduction pipeline", "error": str(exc)}
    result["viz"] = viz
    if viz.get("source"):
        provenance.append(viz["source"])
    result["viz_kind"] = viz.get("kind")
    return result


# ---------------------------------------------------------------------------
# Scene script generation
# ---------------------------------------------------------------------------

SCENE_TEMPLATE = r'''"""
Auto-generated Manim scene for a single reproduced paper.

Rendered by scripts/make_manim.py. It reads ``data.json`` from its own
directory and builds a real, data-driven scientific animation in the 3Blue1Brown
style using the shared primitive library ``manim_scientific`` -- animated Axes
with plotted line / scatter / bar data, plus a geometric transformation
flourish, wrapped by branded title and outro cards. Pango Text only (no LaTeX),
so it renders on a CPU-only machine.

Every stage is wrapped in try/except so bad data can never crash the render.

Render manually with, e.g.:
    manim render scene.py __SCENE_CLASS__ -qm --format mp4
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from manim import (
    Scene, MovingCameraScene, Axes, NumberPlane, NumberLine, Text, VGroup, VMobject,
    Rectangle, RoundedRectangle, Line, DashedLine, Dot, Arrow, Vector,
    Polygon, RegularPolygon, Circle, Square, Triangle, TracedPath,
    UP, DOWN, LEFT, RIGHT, UL, UR, DL, DR, ORIGIN, PI, TAU,
    FadeIn, FadeOut, Write, GrowFromEdge, GrowArrow, Create, DrawBorderThenFill,
    Transform, ReplacementTransform, MoveAlongPath,
    Indicate, Flash, Circumscribe,
    ValueTracker, always_redraw, smooth, there_and_back,
)

# --- brand theme (inline, so the scene renders even without the library) ----
BG = "#0A0E1A"
PRIMARY = "#E8ECF6"
MUTED = "#8A93AD"
ACCENT = "#6D7BFF"
ACCENT2 = "#34E0E0"
GRID = "#1B2236"
AREA_COLORS = {"AI": "#A78BFA", "DS": "#22D3EE", "ML": "#34D399", "DL": "#FBBF24"}

# Make the shared branded-primitive library importable (path injected by
# make_manim.py). If it is unavailable we fall back to a minimal inline clip.
sys.path.insert(0, r"__SCRIPTS_DIR__")
try:
    import manim_scientific as ms
    _LIB = True
except Exception:
    _LIB = False

DATA = json.loads((Path(__file__).parent / "data.json").read_text(encoding="utf-8"))


def _acol(area: str) -> str:
    return AREA_COLORS.get((area or "").upper(), ACCENT)


class __SCENE_CLASS__(MovingCameraScene):
    def construct(self):
        self.camera.background_color = BG
        self.area = (DATA.get("area") or "").upper()
        self.acol = _acol(self.area)

        # Remember the camera's home frame so primitives can zoom + return.
        try:
            self._home_w = float(self.camera.frame.width)
        except Exception:
            self._home_w = None
        try:
            if _LIB:
                ms.save_camera_home(self)
        except Exception:
            pass

        # persistent dotted backdrop
        try:
            self.backdrop = ms.backdrop(self) if _LIB else self._inline_backdrop()
        except Exception:
            self.backdrop = VGroup()
            self.add(self.backdrop)

        if _LIB:
            self._stage(self._title)
            self._stage(self._viz)
            self._stage(self._flourish)
            self._stage(self._outro)
        else:
            self._stage(self._inline_card)
            self._stage(self._flourish)

    # -- stage runner: isolates + cleans up each stage ----------------------
    def _stage(self, fn):
        try:
            fn()
        except Exception:
            self._clear_dynamic()

    def _clear_dynamic(self):
        keep = getattr(self, "backdrop", None)
        for m in list(self.mobjects):
            if keep is not None and m is keep:
                continue
            self.remove(m)
        # If a stage failed mid-zoom, snap the camera home so the next stage
        # is framed correctly (instant, no animation).
        frame = getattr(self.camera, "frame", None)
        if frame is not None and getattr(self, "_home_w", None):
            try:
                frame.set(width=self._home_w).move_to(ORIGIN)
            except Exception:
                pass

    def _inline_backdrop(self):
        dots = VGroup()
        x = -6.8
        while x <= 6.8 + 1e-6:
            y = -3.7
            while y <= 3.7 + 1e-6:
                d = Dot(point=[x, y, 0], radius=0.013, color=GRID)
                d.set_opacity(0.5)
                dots.add(d)
                y += 0.62
            x += 0.62
        self.add(dots)
        return dots

    # -- 1) TITLE CARD ------------------------------------------------------
    def _title(self):
        ms.title_card(self, DATA.get("title") or "Research finding", self.area)

    # -- 2) DATA-DRIVEN VISUALISATION --------------------------------------
    def _viz(self):
        viz = DATA.get("viz") or {}
        kind = viz.get("kind")
        head = None
        if kind in ("scatter", "line", "grouped_bars", "bars"):
            head = ms.section_label(self, self._viz_heading(kind), self.area)
        created = None
        try:
            if kind == "scatter":
                created = ms.animated_scatter(
                    self, viz.get("xs", []), viz.get("ys", []),
                    x_label=viz.get("x_label", ""), y_label=viz.get("y_label", ""),
                    area=self.area, diagonal=bool(viz.get("diagonal")))
            elif kind == "line":
                created = ms.animated_line_plot(
                    self, viz.get("series", []),
                    x_label=viz.get("x_label", ""), y_label=viz.get("y_label", ""),
                    area=self.area)
            elif kind == "grouped_bars":
                created = ms.grouped_bar_compare(
                    self, viz.get("labels", []), viz.get("paper", []),
                    viz.get("repro", []), area=self.area,
                    paper_name=viz.get("paper_name", "Paper"),
                    repro_name=viz.get("repro_name", "Reproduced"),
                    normalize=viz.get("normalize", "global"))
            elif kind == "bars":
                created = ms.show_bars(self, viz.get("labels", []),
                                       viz.get("values", []), area=self.area)
            else:
                created = ms.conceptual_diagram(
                    self, viz.get("nodes", []), area=self.area,
                    title=viz.get("title", ""))
        except Exception:
            self._clear_dynamic()
            created = ms.conceptual_diagram(
                self, viz.get("nodes") or ["Paper", "Reproduce", "Result"],
                area=self.area, title="Reproduction")

        caption = None
        cap_text = viz.get("caption") or DATA.get("finding") or ""
        if cap_text:
            lines = ms.wrap(cap_text, 64, 2)
            caption = VGroup(*[ms.T(ln, color=MUTED, font_size=22) for ln in lines])
            caption.arrange(DOWN, buff=0.12)
            ms.fit(caption, ms.FRAME_W)
            caption.to_edge(DOWN, buff=0.35)
            self.play(FadeIn(caption, shift=UP * 0.1), run_time=0.6)

        self.wait(1.6)
        fade = [m for m in (head, created, caption) if m is not None]
        if fade:
            self.play(*[FadeOut(m) for m in fade], run_time=0.6)

    def _viz_heading(self, kind: str) -> str:
        return {"scatter": "Reproduced vs paper", "line": "Reproduced curves",
                "grouped_bars": "Reproduced vs reference",
                "bars": "Reproduced metrics"}.get(kind, "Reproduced result")

    # -- 3) GEOMETRIC / TRANSFORMATION FLOURISH (inline graphics) ----------
    def _camera(self):
        return getattr(self.camera, "frame", None)

    def _flourish(self):
        # A "reproduction = convergence" metaphor: a ball performs gradient
        # descent down a loss curve (ValueTracker roll + discrete step markers +
        # a fading TracedPath), the camera pushes in on the minimum, then a
        # triangle morphs through a hexagon into a circle (multi-step Transform).
        col = self.acol
        plane = NumberPlane(
            x_range=[-4, 4, 1], y_range=[-2.2, 2.2, 1],
            x_length=10.5, y_length=5.4,
            background_line_style={"stroke_color": GRID, "stroke_width": 1,
                                   "stroke_opacity": 0.4},
            axis_config={"stroke_color": MUTED, "stroke_width": 2,
                         "include_numbers": False, "include_tip": False},
        ).move_to(ORIGIN)
        self.play(Create(plane), run_time=0.7)

        ax = Axes(x_range=[-3, 3, 1], y_range=[0, 4, 1],
                  x_length=9.6, y_length=4.4,
                  axis_config={"stroke_opacity": 0}).move_to(DOWN * 0.4)

        def _loss(x):
            return 0.42 * x * x + 0.18 * math.sin(2.4 * x) + 0.55

        graph = ax.plot(_loss, x_range=[-3, 3, 0.03], color=MUTED,
                        stroke_width=4)
        self.play(Create(graph), run_time=1.0)

        # Smooth roll of the ball down to the basin, leaving a comet trail.
        tracker = ValueTracker(-2.7)
        ball = always_redraw(lambda: Dot(
            ax.c2p(tracker.get_value(), _loss(tracker.get_value())),
            color=col, radius=0.12))
        trail = None
        try:
            trail = TracedPath(ball.get_center, stroke_color=ACCENT2,
                               stroke_width=5, dissipating_time=0.55)
            self.add(trail)
        except Exception:
            trail = None
        self.add(ball)
        self.play(tracker.animate.set_value(0.0), run_time=1.9,
                  rate_func=smooth)

        # Discrete gradient-descent hops converging on the minimum.
        try:
            x = -2.4
            steps = VGroup()
            prev = ax.c2p(x, _loss(x))
            for _ in range(5):
                x -= 0.5 * (0.84 * x + 0.43 * math.cos(2.4 * x))  # ~ -lr*grad
                cur = ax.c2p(x, _loss(x))
                hop = Dot(prev, color=PRIMARY, radius=0.05)
                arr = Arrow(prev, cur, color=PRIMARY, buff=0.02,
                            stroke_width=3, max_tip_length_to_length_ratio=0.25)
                steps.add(hop, arr)
                prev = cur
            self.play(Create(steps, lag_ratio=0.5), run_time=1.3)
        except Exception:
            steps = VGroup()

        minpt = Dot(ax.c2p(0.0, _loss(0.0)), color=ACCENT2, radius=0.1)
        conv = ms.T("converged", color=ACCENT2, font_size=24) if _LIB \
            else Text("converged", color=ACCENT2, font_size=24)
        conv.next_to(minpt, UP, buff=0.35)

        frame = self._camera()
        zoomed = False
        if frame is not None:
            try:
                self.play(frame.animate.scale(0.55).move_to(minpt.get_center()
                                                            + UP * 0.4),
                          run_time=1.0, rate_func=smooth)
                zoomed = True
            except Exception:
                zoomed = False
        self.play(FadeIn(minpt, scale=0.4), run_time=0.4)
        self.play(Flash(minpt, color=ACCENT2, line_length=0.3), run_time=0.5)
        self.play(FadeIn(conv, shift=UP * 0.1), run_time=0.4)
        self.wait(0.3)
        if zoomed and frame is not None:
            try:
                w = self._home_w or 14.222
                self.play(frame.animate.move_to(ORIGIN).set(width=w),
                          run_time=0.9, rate_func=smooth)
            except Exception:
                pass

        # Clear the live updaters so the closing FadeOut actually fades them
        # (an always_redraw / TracedPath mobject would otherwise regenerate
        # itself every frame and ignore the opacity animation).
        for m in (ball, trail):
            if m is not None:
                try:
                    m.clear_updaters()
                except Exception:
                    pass
        descent = VGroup(graph, ball, minpt, conv, steps)
        if trail is not None:
            descent.add(trail)
        self.play(FadeOut(descent), run_time=0.5)

        # multi-step morph: triangle -> hexagon -> circle (Transform sequence)
        tri = Triangle(color=ACCENT2, stroke_width=6).scale(0.9)
        tri.set_fill(ACCENT2, opacity=0.12).move_to(ORIGIN)
        self.play(DrawBorderThenFill(tri), run_time=0.7)
        hexa = RegularPolygon(n=6, color=col, stroke_width=6).scale(1.15)
        hexa.set_fill(col, opacity=0.12).move_to(ORIGIN)
        self.play(Transform(tri, hexa), run_time=0.8, rate_func=smooth)
        circ = Circle(radius=1.25, color=PRIMARY, stroke_width=6)
        circ.set_fill(PRIMARY, opacity=0.06).move_to(ORIGIN)
        self.play(Transform(tri, circ), run_time=0.8, rate_func=smooth)
        self.play(Indicate(tri, color=col, scale_factor=1.08), run_time=0.6)
        self.wait(0.3)
        self.play(FadeOut(VGroup(plane, ax, tri)), run_time=0.6)

    # -- 4) OUTRO CARD ------------------------------------------------------
    def _outro(self):
        ms.outro_card(self, self.area, DATA.get("date") or DATA.get("generated") or "")
        self.play(FadeOut(getattr(self, "backdrop", VGroup())), run_time=0.5)

    # -- minimal fallback if the library could not be imported --------------
    def _inline_card(self):
        title = Text(str(DATA.get("title") or "Research finding"),
                     color=PRIMARY, weight="BOLD", font_size=42)
        if title.width > 12.0:
            title.scale(12.0 / title.width)
        rule = Line(LEFT, RIGHT, color=self.acol, stroke_width=5)
        rule.set_length(min(title.width, 12.0))
        block = VGroup(title, rule).arrange(DOWN, buff=0.3).move_to(ORIGIN)
        self.play(Write(title), run_time=1.2)
        self.play(Create(rule), run_time=0.5)
        self.wait(1.2)
        self.play(FadeOut(block), run_time=0.6)
'''


def _write_scene(manim_dir: Path, data: dict[str, Any]) -> Path:
    """Write data.json + the scene .py into <paper>/manim/. Returns the .py path."""
    manim_dir.mkdir(parents=True, exist_ok=True)
    (manim_dir / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    scene_path = manim_dir / "scene.py"
    scripts_posix = SCRIPTS_DIR.as_posix()  # forward slashes: safe in a raw string
    scene_src = (SCENE_TEMPLATE
                 .replace("__SCENE_CLASS__", SCENE_CLASS)
                 .replace("__SCRIPTS_DIR__", scripts_posix))
    scene_path.write_text(scene_src, encoding="utf-8")
    return scene_path


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _manim_importable() -> tuple[bool, str]:
    try:
        import manim  # noqa: F401
        return True, getattr(manim, "__version__", "unknown")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _has_real_ffmpeg() -> bool:
    """True only if an executable literally named ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


def _render_attempts() -> list[tuple[str, list[str]]]:
    """Ordered (format, extra CLI args) attempts, best first, degrading gracefully."""
    if _has_real_ffmpeg():
        return [
            ("mp4", ["--format", "mp4"]),
            ("gif", ["--format", "gif"]),
            ("png", ["-s", "--format", "png"]),
        ]
    return [("png", ["-s", "--format", "png"])]


def _run_manim(scene_file: Path, media_dir: Path, out_name: str,
               extra: list[str], log_path: Path, timeout_s: int) -> tuple[bool, str]:
    """Invoke ``python -m manim`` as a subprocess (isolates crashes)."""
    cmd = [
        sys.executable, "-m", "manim", "render",
        str(scene_file), SCENE_CLASS,
        "-qm",  # medium quality (720p) -- crisp but still CPU friendly
        "--fps", str(RENDER_FPS),  # trim frame count to stay within the timeout
        "--media_dir", str(media_dir),
        "-o", out_name,
        "--disable_caching",
        *extra,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(scene_file.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout_s, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        return False, f"manim executable not found: {exc}"
    except subprocess.TimeoutExpired:
        return False, f"manim render timed out after {timeout_s}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"manim render raised: {exc}"

    try:
        log_path.write_text(proc.stdout or "", encoding="utf-8", errors="replace")
    except Exception:
        pass
    return proc.returncode == 0, (proc.stdout or "")[-1200:]


def _collect_output(media_dir: Path, ext: str) -> Path | None:
    """Find the newest rendered artifact of the given extension under media_dir."""
    candidates = sorted(media_dir.rglob(f"*.{ext}"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def render_for_paper(paper_dir: str | Path, name: str = DEFAULT_OUTPUT_NAME,
                     timeout_s: int = 300) -> dict[str, Any]:
    """
    Generate + render a Manim animation for one reproduced paper folder.

    ALWAYS writes the scene script (so a later run can render it even if Manim
    is unavailable right now). Rendering is wrapped in try/except and every
    failure mode is recorded in the returned dict rather than raised.
    """
    paper_dir = Path(paper_dir).resolve()
    record: dict[str, Any] = {
        "paper_dir": str(paper_dir),
        "status": "failed",
        "scene": None,
        "output": None,
        "format": None,
        "manim_available": False,
        "manim_version": "",
        "viz_kind": None,
        "deviations": [],
        "error": None,
        "provenance": [],
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    if not paper_dir.exists():
        record["error"] = f"paper_dir does not exist: {paper_dir}"
        return record

    # 1) Derive the finding + data-driven viz spec; write the scene script.
    try:
        data = _extract_finding(paper_dir)
        record["provenance"] = data.get("provenance", [])
        record["viz_kind"] = data.get("viz_kind")
        manim_dir = paper_dir / "manim"
        scene_file = _write_scene(manim_dir, data)
        record["scene"] = str(scene_file)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"scene generation failed: {exc}"
        return record

    # 2) Is Manim importable right now? If not, keep the script and note it.
    available, version = _manim_importable()
    record["manim_available"] = available
    record["manim_version"] = version
    if not available:
        record["status"] = "script-only"
        record["error"] = f"manim not importable (install may still be finishing): {version}"
        record["deviations"].append(
            "Wrote scene.py + data.json but skipped rendering because Manim could not be imported."
        )
        return record

    if not _has_real_ffmpeg():
        record["deviations"].append(
            "ffmpeg not found on PATH; rendered a still PNG frame instead of an MP4/GIF video."
        )

    # 3) Render, degrading gracefully across formats.
    media_dir = manim_dir / "_media"
    log_path = manim_dir / "render.log"
    last_err = ""
    for fmt, extra in _render_attempts():
        try:
            ok, tail = _run_manim(scene_file, media_dir, name, extra, log_path, timeout_s)
        except Exception as exc:  # noqa: BLE001
            last_err = f"{fmt}: {exc}"
            continue
        if not ok:
            last_err = f"{fmt}: {tail}"
            continue
        produced = _collect_output(media_dir, fmt)
        if not produced:
            last_err = f"{fmt}: render reported success but no .{fmt} artifact was found"
            continue
        final = manim_dir / f"{name}.{fmt}"
        try:
            shutil.copyfile(produced, final)
        except Exception as exc:  # noqa: BLE001
            last_err = f"{fmt}: could not copy artifact: {exc}"
            continue
        record.update(status="ok", output=str(final), format=fmt, error=None)
        return record

    record["status"] = "failed"
    record["error"] = f"all render attempts failed. last: {last_err[-800:]}"
    return record


# ---------------------------------------------------------------------------
# Batch + smoke test
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def iter_paper_dirs(repo_root: Path) -> list[Path]:
    """Every reproduced paper folder: <repo>/{AI,DL,DS,ML}/<slug>."""
    out: list[Path] = []
    for area in ("AI", "DL", "DS", "ML"):
        adir = repo_root / area
        if not adir.is_dir():
            continue
        for child in sorted(adir.iterdir()):
            if not child.is_dir():
                continue
            if (child / "paper.md").exists() or (child / "REPRODUCTION.md").exists() \
                    or (child / "summary.md").exists() or (child / "results").is_dir() \
                    or (child / "reproduced_results").is_dir():
                out.append(child)
    return out


def smoke_test() -> dict[str, Any]:
    """
    Minimal example scene: seed a throwaway paper folder with a tiny finding +
    reproduced-vs-paper metrics and attempt a real render. Exercises the whole
    data-driven graphical path without depending on a harvested paper.
    """
    tmp = Path(tempfile.mkdtemp(prefix="manim_smoke_"))
    paper = tmp / "DL" / "example-smoke-scene"
    (paper / "reproduced_results").mkdir(parents=True, exist_ok=True)
    (paper / "paper.md").write_text(
        "# Example: Faster Convergence with Layer-wise Scaling\n\n"
        "## Abstract\nA minimal synthetic demonstration used as a render smoke test.\n",
        encoding="utf-8",
    )
    (paper / "summary.md").write_text(
        "# Key finding\nThe scaled method matches the paper's reported accuracy "
        "across metrics while using far fewer steps.\n",
        encoding="utf-8",
    )
    # A small paper-vs-reproduced metric table -> reproduced-vs-paper scatter.
    (paper / "reproduced_results" / "metrics.json").write_text(
        json.dumps({
            "acc": {"paper": 0.912, "reproduced": 0.905},
            "f1": {"paper": 0.884, "reproduced": 0.878},
            "precision": {"paper": 0.90, "reproduced": 0.897},
            "recall": {"paper": 0.87, "reproduced": 0.861},
            "auc": {"paper": 0.941, "reproduced": 0.933},
            "map": {"paper": 0.802, "reproduced": 0.795},
            "convergence_curve": {"steps": [0, 1, 2, 3, 4, 5],
                                  "loss": [1.8, 1.1, 0.7, 0.45, 0.32, 0.27]},
        }),
        encoding="utf-8",
    )
    rec = render_for_paper(paper, name="smoke")
    rec["smoke_tmp"] = str(tmp)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-paper Manim animation generator.")
    ap.add_argument("--paper", type=Path, default=None, help="a single reproduced paper folder")
    ap.add_argument("--backfill", action="store_true", help="render for every reproduced paper in the repo")
    ap.add_argument("--smoke", action="store_true", help="render a minimal example scene (self-test)")
    ap.add_argument("--name", default=DEFAULT_OUTPUT_NAME, help="output basename (default: finding)")
    ap.add_argument("--timeout", type=int, default=300, help="per-render timeout in seconds")
    args = ap.parse_args()

    if args.smoke:
        rec = smoke_test()
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0 if rec["status"] in ("ok", "script-only") else 1

    if args.paper:
        rec = render_for_paper(args.paper, name=args.name, timeout_s=args.timeout)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0 if rec["status"] in ("ok", "script-only") else 1

    if args.backfill:
        cfg = _load_config()
        repo_root = Path(cfg.get("paths", {}).get("repo_root") or REPO_ROOT)
        papers = iter_paper_dirs(repo_root)
        print(f"[make_manim] {len(papers)} paper folder(s) found")
        results = []
        for p in papers:
            rec = render_for_paper(p, name=args.name, timeout_s=args.timeout)
            results.append(rec)
            print(f"  {rec['status']:11} {rec.get('format') or '-':4} "
                  f"{rec.get('viz_kind') or '-':12} {p}")
        ok = sum(1 for r in results if r["status"] == "ok")
        print(f"[make_manim] done: {ok}/{len(results)} rendered")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
