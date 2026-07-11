#!/usr/bin/env python3
"""
Per-paper reproduction harness.

For every freshly-harvested paper that has NOT yet been reproduced (deduplicated
against the append-only ledger  state/processed_ledger.jsonl) it:

  1. scaffolds the canonical per-paper repo under
        <repo>/<AREA>/<paper-slug>/
     with the fixed structure:
        src/                reproduced Python code (starts from the paper's own
                            GitHub repo if one is found + cloned)
        original_data/      authors' data (downloaded, or a DATA_SOURCE.md link)
        original_results/   the paper's key figures (from its repo, else
                            extracted from the PDF)
        reproduced_results/ figures/metrics produced by src/
        tests/              pytest unit + result-validation tests
        manim/              manim animation of the core finding
        summary.pdf         methodology + how the code reproduces the results
     and drops in paper.pdf + paper.md.

  2. best-effort locates the paper's OFFICIAL code repo (scanning the paper text
     / landing links, then a GitHub API search) and shallow-clones it into
        src/upstream/
     so the reproduction starts from the authors' own code.

  3. auto-extracts the paper's figures from paper.pdf into original_results/.

  4. drives the reproduction by invoking the local `claude` CLI headlessly
     (bounded per-paper wall-clock, cwd = the paper dir, exactly like the wiki
     ingest runner: `claude -p <prompt> --dangerously-skip-permissions`),
     instructing it to reproduce the paper's MAIN result(s) + KEY figure(s),
     fetch the original data, write src/tests/manim, and write summary.md.

  5. guarantees summary.pdf exists (renders summary.md -> summary.pdf via
     reportlab if claude did not produce the PDF itself).

  6. records the outcome in  state/progress.jsonl  and appends the dedup key to
     state/processed_ledger.jsonl.

Reproducing arbitrary frontier papers fully is not always possible (GPU-scale
training, proprietary data). The harness targets a *faithful minimal
reproduction*: the core method on small/public/synthetic data, key figures
regenerated, unit tests, an honest deviations section, and a manim animation of
the central finding. Papers accumulate day by day.

Constraints honoured: Windows, CPU-only, Python 3.10 in the project .venv. The
GitHub token (read from the environment for API search only) is NEVER printed,
logged, or written to any file. config.json is read-only here.

Usage:
    python reproduce.py --config ../config.json                 # today's harvest
    python reproduce.py --config ../config.json --harvest 2026-06-30
    python reproduce.py --config ../config.json --backfill      # any un-reproduced paper in the corpus
    python reproduce.py --config ../config.json --backfill --deadline-minutes 300
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ensure the shared pdf styling module (scripts/pdf_style.py) is importable
# regardless of the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_paths  # noqa: E402  (needs the sys.path insert above)
import wiki_index  # noqa: E402  (TF-IDF retrieval over the LLM knowledge wiki)

# -----------------------------------------------------------------------------
# canonical per-paper directory layout
# -----------------------------------------------------------------------------
CANONICAL_SUBDIRS = (
    "src",
    "original_data",
    "original_results",
    "reproduced_results",
    "tests",
    "manim",
)

# -----------------------------------------------------------------------------
# the reproduction prompt handed to the headless claude CLI
# -----------------------------------------------------------------------------
PROMPT_TEMPLATE = """You are reproducing a published research paper. Work tirelessly and fully autonomously; never ask questions; never stop early. Everything you write must actually RUN.
If the `ml_ultracode` skill is available, invoke it first and follow its workflow (data-first inspection, explicit metrics, baseline, leakage-safe pipelines, multi-seed validation, honest reporting).

PAPER: {title}
AREA: {area}
Working directory (your cwd) contains: paper.pdf (original) and paper.md (extracted text).
Python interpreter to use for EVERYTHING: {python}
   (invoke it explicitly, e.g.  {python} -m pytest -q  , and add any pip installs to requirements.txt in this directory)
{wiki_note}
GOAL - produce a faithful, runnable reproduction of the paper's MAIN result(s) and KEY figure(s) in THIS directory, using this EXACT fixed structure (all folders already exist):
  src/                 your clean, documented, importable reproduction code + a runnable entrypoint  src/reproduce.py
  original_data/       the authors' original data (see DATA below)
  original_results/    the paper's own key figures (see FIGURES below)
  reproduced_results/  figures + metrics YOUR src/ actually produces (reproduced_results/metrics.json + PNGs). These must be generated by running your code, never hand-written. metrics.json is MANDATORY and MUST follow the schema in the METRICS section below (named paper-vs-reproduced numbers + an overall verdict).
  tests/               pytest tests (test_*.py) that validate the implementation at INTERMEDIATE stages AND the FINAL results
  manim/               a manim animation of the paper's core finding (manim/scene.py with a Scene subclass). Render it to manim/<name>.mp4 or .gif if manim is installed; if manim/ffmpeg is unavailable, keep scene.py runnable and note it in summary.md.
  summary.md           concise, technically-correct methodology write-up (the harness converts this to summary.pdf)

CODE - src/:
{github_note}
Keep the method/architecture/algorithm TRUE to the paper. Make src/reproduce.py the single entrypoint that regenerates everything in reproduced_results/.

DATA - original_data/:
{data_note}

FIGURES - original_results/:
{figures_note}

METRICS - reproduced_results/metrics.json (MANDATORY, machine-readable, produced by running src/):
Write a single JSON object with EXACTLY these top-level keys:
  "paper_title": string
  "metrics": a list of objects, one per headline quantity you compare, each:
       {{"name": short metric name (e.g. "test accuracy", "FID", "chi^2/dof", "AUROC"),
         "paper_value": the number/string the paper reports (or null if the paper gives none),
         "reproduced_value": the number YOUR code produced (never hand-typed - read it from your run),
         "unit": string or null,
         "abs_diff": reproduced-minus-paper as a number, or null if not comparable,
         "within_tolerance": true/false/null (your judgement of whether they agree),
         "notes": one short clause on scaling/caveats}}
  "verdict": one of "full" | "partial" | "minimal" | "infeasible"
  "verdict_reason": one sentence justifying the verdict
  "reproduced_on": ISO date string
Include at least one metric whenever the paper reports ANY quantitative result. If the
paper is purely qualitative, still emit metrics with paper_value=null describing what you
measured. Keep names stable and human-readable - a downstream digest reads this file.

HARD CONSTRAINTS:
- CPU ONLY. No GPU, modest RAM. If the paper needs GPU-scale training or proprietary/huge data, SCALE DOWN FAITHFULLY: a small public or synthetic dataset, fewer epochs/params/layers - but keep the core method intact. Document every deviation.
- Prefer libraries installable via pip (numpy, scipy, scikit-learn, pandas, matplotlib; torch CPU wheels only if essential; manim if you can). Pin nothing exotic. Record installs in requirements.txt.
- Actually EXECUTE src/reproduce.py and the tests with {python}; capture the real outputs into reproduced_results/. Do NOT fabricate numbers or figures - if something could not run, say so explicitly in summary.md.
- Keep total work within your time budget. A correct MINIMAL reproduction beats an unfinished ambitious one.
- NETWORK ETIQUETTE (binding - C:\\Users\\ADMIN\\Agentic_Projects\\NETWORK_ETIQUETTE.md): fetch only through sanctioned channels - `git clone` for repos, library downloaders (torchvision/sklearn/huggingface_hub datasets), official APIs, direct data links from the paper. Never scrape HTML that has an API; web search ONLY via the built-in WebSearch tool. At most ONE download attempt per file, >=3 s between requests to the same host. If a host 403s/429s/captchas or a link is dead: STOP trying that host, note it in summary.md, and use a small public/synthetic stand-in. Never retry through a different User-Agent/IP/route - a skipped download is fine, a bot ban is not.

summary.md MUST be substantive (aim for 400-800 words) and contain these headings IN ORDER:
  1. Central claim - the paper's main result(s) in 2-3 sentences, with the specific numbers it claims.
  2. Method - how the method/algorithm/model works, faithfully and concretely (equations in words, key hyperparameters, the training/eval loop). Enough that a reader understands WHAT you implemented.
  3. Reproduction pipeline - how src/ regenerates everything: the exact command (`{python} src/reproduce.py`), the data it uses, each stage data -> pipeline -> outputs, and which files in reproduced_results/ each stage writes.
  4. Results comparison - a MARKDOWN TABLE with columns | Metric | Paper | Reproduced | Diff | Agree? | drawn from reproduced_results/metrics.json, one row per metric. Reference the reproduced figure files and their original_results/ counterparts. State plainly where you matched and where you did not.
  5. Deviations & scaling-down - every change you made for CPU/data/time limits (smaller dataset, fewer epochs/params, synthetic stand-ins) and WHY each is faithful to the method.
  6. Threats to validity - what could make these numbers misleading (tiny sample, seed sensitivity, missing baseline, un-tuned hyperparameters).
  7. Reproducibility verdict - one of full / partial / minimal / infeasible, matching metrics.json's "verdict", plus a one-sentence reason. Do NOT overclaim; an honest "partial" beats a false "full".

Consistency: the numbers in summary.md's table, in reproduced_results/metrics.json, and in your final message MUST agree. Never fabricate - if a stage could not run, say so explicitly here and set the verdict accordingly.

When finished, stop. Your final message must be a 3-5 line summary of what was reproduced, the headline paper-vs-reproduced numbers, and the verdict.
"""


# -----------------------------------------------------------------------------
# small utilities
# -----------------------------------------------------------------------------

def slugify(title: str, max_len: int = 80) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", title.strip().lower()).strip("-")
    return (s[:max_len].strip("-")) or "paper"


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl_slugs(path: Path) -> set[str]:
    done: set[str] = set()
    if path.exists():
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            slug = obj.get("slug")
            if slug:
                done.add(slug)
    return done


def already_processed(ledger_path: Path, progress_path: Path) -> set[str]:
    """Dedup keys: union of the processed ledger and the progress log."""
    return _load_jsonl_slugs(ledger_path) | _load_jsonl_slugs(progress_path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# -----------------------------------------------------------------------------
# scaffolding
# -----------------------------------------------------------------------------

def scaffold(paper_dir: Path, pdf: str | None, md: str | None) -> None:
    for sub in CANONICAL_SUBDIRS:
        (paper_dir / sub).mkdir(parents=True, exist_ok=True)
    if pdf and Path(pdf).exists() and not (paper_dir / "paper.pdf").exists():
        shutil.copyfile(pdf, paper_dir / "paper.pdf")
    if md and Path(md).exists() and not (paper_dir / "paper.md").exists():
        shutil.copyfile(md, paper_dir / "paper.md")


# -----------------------------------------------------------------------------
# GitHub repo discovery + shallow clone
# -----------------------------------------------------------------------------

_GH_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_GH_BAD_OWNERS = {"sponsors", "features", "about", "topics", "marketplace",
                  "orgs", "settings", "notifications", "explore"}


def _clean_repo(owner: str, repo: str) -> tuple[str, str] | None:
    repo = repo.strip().rstrip(".,);:'\"]}>")
    if repo.endswith(".git"):
        repo = repo[:-4]
    owner = owner.strip()
    if not owner or not repo:
        return None
    if owner.lower() in _GH_BAD_OWNERS:
        return None
    if repo.lower() in {"blob", "tree", "raw"}:
        return None
    return owner, repo


def discover_github_urls(md_text: str, rec: dict[str, Any]) -> list[str]:
    """Scan the paper text + landing links for candidate official-code repos."""
    hay = md_text or ""
    for key in ("landing_url", "pdf_url"):
        v = rec.get(key)
        if v:
            hay += "\n" + str(v)
    seen: list[str] = []
    for m in _GH_RE.finditer(hay):
        cleaned = _clean_repo(m.group(1), m.group(2))
        if not cleaned:
            continue
        url = f"https://github.com/{cleaned[0]}/{cleaned[1]}"
        if url not in seen:
            seen.append(url)
    return seen


def _github_headers() -> dict[str, str]:
    """Authorization header from the environment token (NEVER logged/written).

    The User-Agent is set by the shared polite client (mailto contact +
    "+reproduce" suffix), not here (NETWORK_ETIQUETTE.md).
    """
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_ACCESS_TOKEN")
    h = {"Accept": "application/vnd.github+json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def search_github_by_title(title: str) -> list[str]:
    """Best-effort: query the GitHub search API for the paper's official repo.

    Routed through polite_http: Search API throttled to >= 2.5 s between
    requests, x-ratelimit-* headers honored, Retry-After-aware backoff.
    """
    try:
        import polite_http  # shared polite client (scripts/ is on sys.path)
    except Exception:  # noqa: BLE001
        return []
    words = re.findall(r"[A-Za-z0-9]+", title)
    q = " ".join(words[:8])
    if not q:
        return []
    try:
        r = polite_http.get(
            "https://api.github.com/search/repositories",
            params={"q": q, "sort": "stars", "order": "desc", "per_page": 3},
            headers=_github_headers(),
            timeout=30,
            ua_suffix="+reproduce",
        )
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
    except polite_http.ProviderBlocked as exc:
        # rule 3: GitHub API blocked -> skip search for this run, keep going
        print(f"[net] {exc} (GitHub search skipped)")
        return []
    except Exception:  # noqa: BLE001
        return []
    return [it["html_url"] for it in items if it.get("html_url")]


def verify_repo(url: str) -> bool:
    """Best-effort existence check via the GitHub API (token used, never logged)."""
    try:
        import polite_http  # shared polite client (scripts/ is on sys.path)
    except Exception:  # noqa: BLE001
        return True  # can't verify -> let the clone decide
    m = _GH_RE.match(url)
    if not m:
        return False
    owner, repo = m.group(1), m.group(2)
    try:
        r = polite_http.get(f"https://api.github.com/repos/{owner}/{repo}",
                            headers=_github_headers(), timeout=30,
                            ua_suffix="+reproduce")
        return r.status_code == 200
    except polite_http.ProviderBlocked:
        return True  # API blocked for this run -> let the git-protocol clone decide
    except Exception:  # noqa: BLE001
        return True


def clone_repo(url: str, dest: Path) -> tuple[bool, str]:
    """Shallow-clone url into dest. GIT_TERMINAL_PROMPT=0 avoids credential hangs.

    Public paper repos clone without credentials; we deliberately do NOT inject
    the token into the clone URL so it can never leak into .git/config.
    """
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=240,
        )
    except FileNotFoundError:
        return (False, "git-not-found")
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        return (False, "clone-timeout")
    if proc.returncode == 0 and dest.exists():
        # drop the .git dir; we want the source as a starting point, not a submodule
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return (True, "cloned")
    shutil.rmtree(dest, ignore_errors=True)
    return (False, f"clone-failed({proc.returncode})")


def locate_and_clone_repo(paper_dir: Path, md_text: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Find the paper's official repo and clone it into src/upstream/."""
    info: dict[str, Any] = {"candidates": [], "cloned_url": None, "clone_note": None}
    candidates = discover_github_urls(md_text, rec)
    if not candidates:
        candidates = search_github_by_title(rec.get("title", ""))
    info["candidates"] = candidates
    dest = paper_dir / "src" / "upstream"
    for url in candidates:
        if not verify_repo(url):
            continue
        ok, note = clone_repo(url, dest)
        info["clone_note"] = note
        if ok:
            info["cloned_url"] = url
            break
    return info


# -----------------------------------------------------------------------------
# original-figure extraction from the PDF
# -----------------------------------------------------------------------------

def extract_pdf_figures(pdf_path: Path, out_dir: Path, max_images: int = 40,
                        min_pixels: int = 90 * 90) -> int:
    """Extract the paper's ORIGINAL figures from paper.pdf into original_results/.

    Delegates to :mod:`figure_extract`, which captures BOTH vector figures
    (matplotlib/plot-style, found by anchoring on caption lines and rendering the
    page region above each caption) AND large embedded rasters, writing captioned
    ``fig-<NN>-<slug>.png`` files plus ``captions.json``. Falls back to the legacy
    embedded-raster-only extractor if that module cannot be imported. Returns the
    number of images written. Never raises (the pipeline runs unattended).

    Public signature preserved for backwards compatibility.
    """
    if not pdf_path.exists():
        return 0
    # Preferred path: caption-aware vector + raster extraction.
    try:
        import figure_extract  # scripts/ is on sys.path
        info = figure_extract.extract_figures(
            pdf_path, out_dir, dpi=200, max_figures=max_images,
            min_raster_pixels=min_pixels)
        return int(info.get("count", 0))
    except Exception:  # noqa: BLE001
        pass  # fall through to the legacy embedded-raster extractor
    return _extract_embedded_rasters_legacy(pdf_path, out_dir, max_images, min_pixels)


def _extract_embedded_rasters_legacy(pdf_path: Path, out_dir: Path,
                                     max_images: int = 40,
                                     min_pixels: int = 90 * 90) -> int:
    """Legacy fallback: embedded raster figures only (no caption/vector support)."""
    if not pdf_path.exists():
        return 0
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    seen_xrefs: set[int] = set()
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return 0
    try:
        for pno in range(doc.page_count):
            if written >= max_images:
                break
            page = doc[pno]
            for img in page.get_images(full=True):
                if written >= max_images:
                    break
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    ext = doc.extract_image(xref)
                except Exception:  # noqa: BLE001
                    continue
                w, h = ext.get("width", 0), ext.get("height", 0)
                if w * h < min_pixels:
                    continue
                imgext = ext.get("ext", "png")
                fname = out_dir / f"fig_p{pno + 1:02d}_{xref}.{imgext}"
                try:
                    fname.write_bytes(ext["image"])
                    written += 1
                except Exception:  # noqa: BLE001
                    continue
        if written == 0:
            # fallback: render up to 6 pages as page images
            for pno in range(min(doc.page_count, 6)):
                try:
                    pix = doc[pno].get_pixmap(dpi=120)
                    pix.save(str(out_dir / f"page_{pno + 1:02d}.png"))
                    written += 1
                except Exception:  # noqa: BLE001
                    continue
    finally:
        doc.close()
    # leave a small manifest so downstream steps know what was auto-extracted
    try:
        (out_dir / "EXTRACTION_NOTE.md").write_text(
            "# Auto-extracted figures\n\n"
            f"{written} image(s) were auto-extracted from paper.pdf by the harness.\n"
            "These are the paper's ORIGINAL figures (some may be logos/decorations - "
            "prune those). If key figures are missing, extract them from paper.pdf.\n",
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    return written


# -----------------------------------------------------------------------------
# headless claude invocation (mirrors the wiki-ingest runner)
# -----------------------------------------------------------------------------

def kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill a child process and its descendants (claude spawns node children).

    Uses taskkill on Windows (no process groups); plain kill elsewhere.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.kill()


def run_claude(claude_exe: str, repo_root: Path, paper_dir: Path, prompt: str,
               minutes: int, logf: Path) -> tuple[str, int]:
    """Invoke claude headlessly in paper_dir with a hard timeout. Returns (status, code)."""
    args = [claude_exe, "-p", prompt, "--dangerously-skip-permissions",
            "--add-dir", str(repo_root)]
    with logf.open("w", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(
                args, cwd=str(paper_dir), stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, text=True,
            )
        except FileNotFoundError:
            return ("claude-not-found", -1)
        try:
            proc.wait(timeout=minutes * 60)
            return ("completed", proc.returncode)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            return ("timeout", -2)


# -----------------------------------------------------------------------------
# summary.md -> summary.pdf (reportlab)
# -----------------------------------------------------------------------------

def markdown_to_pdf(md_path: Path, pdf_path: Path) -> bool:
    """Render a reproduction summary markdown file to a branded PDF.

    Delegates all styling to the shared :mod:`pdf_style` module so that every
    summary.pdf across the project is visually identical. Area/title/key-figure
    metadata are derived from the paper directory. Returns True on success.
    """
    try:
        # scripts/ is on sys.path (added at import time); import the shared styler
        from pdf_style import build_summary_pdf, first_h1_title, humanize_title, \
            pick_key_figure
    except Exception:  # noqa: BLE001
        return False

    paper_dir = md_path.parent
    meta = {
        "title": first_h1_title(md_path) or humanize_title(paper_dir.name),
        "area": paper_dir.parent.name,        # AI / DS / ML / DL (or folder name)
        "date": datetime.now().strftime("%Y-%m-%d"),
        "key_figure_path": pick_key_figure(paper_dir),
    }
    try:
        return build_summary_pdf(md_path, pdf_path, meta)
    except Exception:  # noqa: BLE001
        return False


def _write_fallback_summary_md(paper_dir: Path, rec: dict[str, Any],
                               art: dict[str, Any], repo_info: dict[str, Any]) -> None:
    """If claude produced no summary.md, synthesise a minimal honest one."""
    md = paper_dir / "summary.md"
    if md.exists():
        return
    repro = paper_dir / "REPRODUCTION.md"
    if repro.exists():
        try:
            shutil.copyfile(repro, md)
            return
        except Exception:  # noqa: BLE001
            pass
    verdict = art.get("verdict")
    verdict_reason = art.get("verdict_reason")
    lines = [
        f"# Reproduction summary: {rec.get('title', paper_dir.name)}",
        "",
        f"**Area:** {rec.get('area_code', '?')}    ",
        f"**Slug:** {paper_dir.name}    ",
        f"**Generated:** {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "## Status",
        "",
        "This summary was auto-generated by the harness because the reproduction "
        "agent did not leave a `summary.md`. It reports only what artifacts were "
        "found on disk; the reproduction may be incomplete.",
        "",
        "## Artifacts produced",
        "",
        f"- Source files in `src/`: {art.get('src_files', 0)}",
        f"- Original figures in `original_results/`: {art.get('original_figs', 0)} "
        f"({art.get('original_captions', 0)} captioned)",
        f"- Reproduced outputs in `reproduced_results/`: {art.get('reproduced_files', 0)} "
        f"({art.get('reproduced_imgs', 0)} image(s))",
        f"- Metrics recorded (`metrics.json`): "
        f"{'yes, ' + str(art.get('n_metrics', 0)) + ' metric(s)' if art.get('has_metrics') else 'no'}",
        f"- Tests in `tests/`: {art.get('tests', 0)}",
        f"- Manim scenes in `manim/`: {art.get('manim_files', 0)} "
        f"(rendered: {art.get('manim_render', 0)})",
        "",
        "## Results comparison",
        "",
        ("A machine-readable `reproduced_results/metrics.json` is present; see it for "
         "the named paper-vs-reproduced numbers."
         if art.get("has_metrics")
         else "No `metrics.json` was produced, so no paper-vs-reproduced comparison "
              "is available for this run."),
        "",
        "## Official code",
        "",
        (f"Cloned from {repo_info.get('cloned_url')} into `src/upstream/`."
         if repo_info.get("cloned_url")
         else "No official repo was located/cloned automatically."),
        "",
        "## Reproducibility verdict",
        "",
        (f"**{verdict}** - {verdict_reason}" if verdict and verdict_reason
         else f"**{verdict}** (from metrics.json)." if verdict
         else "infeasible/incomplete - the agent run did not finish the write-up. "
              "See the run log referenced in `state/progress.jsonl`."),
        "",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")


def ensure_summary_pdf(paper_dir: Path, rec: dict[str, Any], art: dict[str, Any],
                       repo_info: dict[str, Any]) -> bool:
    """Guarantee summary.pdf exists (rendering summary.md if needed)."""
    pdf = paper_dir / "summary.pdf"
    md = paper_dir / "summary.md"
    if not md.exists():
        _write_fallback_summary_md(paper_dir, rec, art, repo_info)
    # (Re)render if the PDF is missing or older than the markdown.
    if pdf.exists() and md.exists() and pdf.stat().st_mtime >= md.stat().st_mtime:
        return True
    if md.exists():
        return markdown_to_pdf(md, pdf)
    return pdf.exists()


# -----------------------------------------------------------------------------
# artifact assessment
# -----------------------------------------------------------------------------

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".mp4"}


def _count_files(d: Path, exts: set[str] | None = None) -> int:
    if not d.exists():
        return 0
    n = 0
    for p in d.rglob("*"):
        if not p.is_file():
            continue
        if exts is None or p.suffix.lower() in exts:
            n += 1
    return n


def _read_metrics(paper_dir: Path) -> dict[str, Any]:
    """Best-effort read of reproduced_results/metrics.json for the digest.

    Returns {"verdict": str|None, "verdict_reason": str|None, "n_metrics": int}.
    Never raises; tolerates the legacy results.json name and malformed content.
    """
    out: dict[str, Any] = {"verdict": None, "verdict_reason": None, "n_metrics": 0}
    for name in ("metrics.json", "results.json"):
        p = paper_dir / "reproduced_results" / name
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            v = data.get("verdict")
            if isinstance(v, str):
                out["verdict"] = v.strip()[:20]
            vr = data.get("verdict_reason")
            if isinstance(vr, str):
                out["verdict_reason"] = vr.strip()[:280]
            m = data.get("metrics")
            if isinstance(m, list):
                out["n_metrics"] = len(m)
            elif isinstance(m, dict):
                out["n_metrics"] = len(m)
        break
    return out


def _count_captions(paper_dir: Path) -> int:
    """Number of captioned original figures recorded in captions.json (0 if none)."""
    p = paper_dir / "original_results" / "captions.json"
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return len(data) if isinstance(data, dict) else 0
    except Exception:  # noqa: BLE001
        return 0


def assess(paper_dir: Path) -> dict[str, Any]:
    src_files = _count_files(paper_dir / "src", {".py"})
    upstream = (paper_dir / "src" / "upstream").exists()
    original_figs = _count_files(paper_dir / "original_results", _IMG_EXTS)
    original_captions = _count_captions(paper_dir)
    reproduced_files = _count_files(paper_dir / "reproduced_results")
    reproduced_imgs = _count_files(paper_dir / "reproduced_results", _IMG_EXTS)
    has_metrics = (paper_dir / "reproduced_results" / "metrics.json").exists() or \
                  (paper_dir / "reproduced_results" / "results.json").exists()
    metrics_meta = _read_metrics(paper_dir)
    tests = len(list((paper_dir / "tests").glob("test_*.py")))
    manim_files = _count_files(paper_dir / "manim", {".py"})
    manim_render = _count_files(paper_dir / "manim", {".mp4", ".gif"})
    data_files = _count_files(paper_dir / "original_data")
    has_data_source = (paper_dir / "original_data" / "DATA_SOURCE.md").exists()
    has_summary_md = (paper_dir / "summary.md").exists()
    has_summary_pdf = (paper_dir / "summary.pdf").exists()
    has_requirements = (paper_dir / "requirements.txt").exists()
    return {
        "src_files": src_files,
        "has_upstream": upstream,
        "original_figs": original_figs,
        "original_captions": original_captions,
        "reproduced_files": reproduced_files,
        "reproduced_imgs": reproduced_imgs,
        "has_metrics": has_metrics,
        "n_metrics": metrics_meta["n_metrics"],
        "verdict": metrics_meta["verdict"],
        "verdict_reason": metrics_meta["verdict_reason"],
        "tests": tests,
        "manim_files": manim_files,
        "manim_render": manim_render,
        "data_files": data_files,
        "has_data_source": has_data_source,
        "has_summary_md": has_summary_md,
        "has_summary_pdf": has_summary_pdf,
        "has_requirements": has_requirements,
        # back-compat aliases for the older send_report.py digest
        "figures": reproduced_imgs,
        "has_results_json": has_metrics,
    }


# -----------------------------------------------------------------------------
# per-paper orchestration
# -----------------------------------------------------------------------------

def _wiki_note(cfg: dict[str, Any], repo_root: Path, title: str, md_text: str) -> str:
    """Retrieve the wiki concept pages most relevant to this paper and format them
    as a grounding block for the reproduction prompt. Returns '' when disabled,
    when the wiki is empty, or when nothing clears the relevance floor."""
    k = int(cfg.get("reproduce", {}).get("wiki_context_pages", 5))
    if k <= 0:
        return ""
    try:
        wiki = wiki_index.get_wiki_index(repo_root / "AI_DS_ML_DL" / "wiki")
    except Exception:  # noqa: BLE001
        return ""
    if not wiki.ready:
        return ""
    # title + the paper's opening text (abstract/intro) is a strong topic query
    query = f"{title}. {md_text[:1500]}"
    hits = wiki.retrieve(query, k=k)
    if not hits:
        return ""
    lines = [
        f"- {p.title}: {(p.summary or '').strip()[:240]}  "
        f"(full page: AI_DS_ML_DL/wiki/concepts/{p.slug}.md)"
        for p, _s in hits
    ]
    return (
        "BACKGROUND FROM YOUR KNOWLEDGE BASE (the most relevant concept pages from "
        "the maintained LLM wiki - use them to ground terminology, standard method "
        "choices, baselines, and expected result ranges; they are distilled summaries, "
        "NOT this paper, so defer to paper.md wherever they differ):\n"
        + "\n".join(lines) + "\n"
    )


def _build_prompt(rec: dict[str, Any], python_exe: str, repo_root: Path,
                  repo_info: dict[str, Any], n_figs: int, wiki_note: str = "") -> str:
    if repo_info.get("cloned_url"):
        github_note = (
            f"The paper's OFFICIAL code has ALREADY been cloned into src/upstream/ "
            f"(from {repo_info['cloned_url']}). START FROM IT: read it, then port/adapt "
            f"the key parts into clean modules directly under src/ (do not just call into "
            f"upstream blindly - reproduce the method). Reuse its logic and configs where "
            f"they help; scale down anything GPU/data-heavy.")
    elif repo_info.get("candidates"):
        cands = ", ".join(repo_info["candidates"][:5])
        github_note = (
            f"Candidate official repositories were found but not auto-cloned: {cands}. "
            f"Check which (if any) is the paper's real code; if correct, clone it into "
            f"src/upstream/ and adapt from it. Otherwise implement from the paper text.")
    else:
        github_note = (
            "No official code link was found in the paper text. Briefly look for the "
            "paper's official repo (arXiv abstract page / project page / a web search); "
            "if you find it, clone it into src/upstream/ and adapt. Otherwise implement "
            "the method faithfully from paper.md.")

    figures_note = (
        f"{n_figs} image(s) were auto-extracted from paper.pdf into original_results/ "
        f"as captioned files named fig-<NN>-<slug>.png. BOTH vector plots (matplotlib "
        f"-style, rendered from the page region above each 'Figure N' caption) AND "
        f"embedded rasters are captured. original_results/captions.json maps each file "
        f"to its caption text - use it to identify which figure is which and to pick the "
        f"KEY figure(s) to reproduce. See original_results/EXTRACTION_NOTE.md. Verify the "
        f"crops are the paper's real figures; delete any decorative/duplicate/mis-cropped "
        f"ones. If the paper's official repo has a figures/ or results dir, prefer those. "
        f"If a KEY figure is still missing or poorly cropped, re-extract it from paper.pdf. "
        f"original_results/ must end up holding the paper's real key figures.")

    data_note = (
        "Put the authors' ORIGINAL data in original_data/. Follow data links in paper.pdf "
        "and the official repo's data/ dir. If the dataset is openly available and small, "
        "download it into original_data/. If it is huge/proprietary/gated, DO NOT download "
        "it - instead write original_data/DATA_SOURCE.md with the exact URL(s), size, "
        "license, and access instructions, and use a small public or synthetic stand-in "
        "for the actual reproduction (document this in summary.md).")

    return PROMPT_TEMPLATE.format(
        title=rec.get("title", "Untitled"),
        area=rec.get("area_code", "?"),
        python=python_exe,
        github_note=github_note,
        figures_note=figures_note,
        data_note=data_note,
        wiki_note=wiki_note,
    )


def reproduce_one(cfg: dict[str, Any], rec: dict[str, Any], repo_root: Path,
                  ledger_path: Path) -> dict[str, Any]:
    area = rec["area_code"]
    title = rec["title"]
    slug = slugify(title)
    paper_dir = repo_root / area / slug

    # 1. scaffold canonical structure + drop in paper.pdf / paper.md
    scaffold(paper_dir, rec.get("local_pdf"), rec.get("local_markdown"))

    # read extracted text for repo/data discovery
    md_text = ""
    md_local = paper_dir / "paper.md"
    if md_local.exists():
        md_text = md_local.read_text(encoding="utf-8", errors="replace")

    # 2. locate + clone the paper's official repo into src/upstream/
    repo_info = locate_and_clone_repo(paper_dir, md_text, rec)

    # 3. extract the paper's original figures from the PDF
    n_figs = extract_pdf_figures(paper_dir / "paper.pdf", paper_dir / "original_results")

    # 4. drive the reproduction via headless claude
    python_exe = pipeline_paths.python_exe(cfg)
    wiki_note = _wiki_note(cfg, repo_root, title, md_text)
    if wiki_note:
        print(f"    [wiki] injected {wiki_note.count(chr(10)) - 1} related concept page(s) "
              f"into the reproduction prompt")
    prompt = _build_prompt(rec, python_exe, repo_root, repo_info, n_figs, wiki_note)
    minutes = cfg.get("reproduce", {}).get("per_paper_minutes", 35)
    claude_exe = pipeline_paths.claude_exe(cfg)
    logs = repo_root / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logf = logs / f"reproduce-{slug[:40]}-{stamp}.log"

    started = datetime.now()
    status, code = run_claude(claude_exe, repo_root, paper_dir, prompt, minutes, logf)
    elapsed = int((datetime.now() - started).total_seconds())

    # 5. assess + guarantee summary.pdf
    art = assess(paper_dir)
    ensure_summary_pdf(paper_dir, rec, art, repo_info)
    art = assess(paper_dir)  # re-assess (summary.pdf/md may now exist)

    produced = (
        art["src_files"] > 0
        and (art["reproduced_files"] > 0 or art["has_metrics"])
        and art["has_summary_pdf"]
    )

    record = {
        "slug": slug, "area": area, "title": title,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "run_status": status, "exit_code": code, "elapsed_s": elapsed,
        "github_repo": repo_info.get("cloned_url"),
        "github_candidates": repo_info.get("candidates", []),
        "artifacts": art, "produced": produced,
        "log": str(logf), "paper_dir": str(paper_dir),
    }

    # 6. append the dedup ledger so this paper is never re-attempted blindly
    append_jsonl(ledger_path, {
        "slug": slug, "area": area, "title": title,
        "date": record["date"], "produced": produced,
        "run_status": status, "github_repo": repo_info.get("cloned_url"),
    })
    return record


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def gather_todo(repo_root: Path, harvest_files: list[Path], done: set[str],
                cap: int) -> list[dict[str, Any]]:
    todo: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for hf in harvest_files:
        if not hf.exists():
            continue
        try:
            data = json.loads(hf.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] bad harvest file {hf}: {exc}")
            continue
        for rec in data.get("records", []):
            if rec.get("status") != "added":
                continue
            slug = slugify(rec.get("title", ""))
            if slug in done or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            todo.append(rec)
    return todo[:cap]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Per-paper reproduction harness (invokes claude headlessly).")
    here = Path(__file__).resolve().parent
    ap.add_argument("--config", type=Path, default=here.parent / "config.json")
    ap.add_argument("--harvest", type=str, default=None,
                    help="harvest date YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill", action="store_true",
                    help="reproduce any un-reproduced paper found in state/harvests")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--deadline-minutes", type=int, default=None,
                    help="stop launching new papers after this wall-clock budget "
                         "(rest backfill next run)")
    args = ap.parse_args()

    if not args.config.exists():
        print(f"[fatal] config not found: {args.config}")
        return 2
    cfg = load_config(args.config)  # read-only; never modified here
    repo_root = pipeline_paths.repo_root(cfg)
    state = repo_root / "state"
    progress_path = state / "progress.jsonl"
    ledger_path = state / "processed_ledger.jsonl"
    done = already_processed(ledger_path, progress_path)

    hdir = state / "harvests"
    if args.backfill:
        harvest_files = sorted(hdir.glob("harvest-*.json"))
    else:
        date = args.harvest or datetime.now().strftime("%Y-%m-%d")
        harvest_files = [hdir / f"harvest-{date}.json"]

    cap = args.max if args.max is not None else cfg["reproduce"].get("max_papers_per_day", 30)
    todo = gather_todo(repo_root, harvest_files, done, cap)
    print(f"[reproduce] {len(todo)} paper(s) to attempt "
          f"(cap={cap}, already-processed={len(done)})")

    summary = {"date": datetime.now().strftime("%Y-%m-%d"),
               "attempted": 0, "produced": 0, "records": []}
    started_all = datetime.now()
    for i, rec in enumerate(todo, 1):
        if args.deadline_minutes is not None:
            spent = (datetime.now() - started_all).total_seconds() / 60
            if spent >= args.deadline_minutes:
                print(f"[reproduce] wall-clock budget {args.deadline_minutes}m reached "
                      f"after {i - 1} papers; remaining {len(todo) - (i - 1)} will "
                      f"backfill next run.")
                break
        print(f"\n[{i}/{len(todo)}] {rec['area_code']} :: {rec['title'][:90]}")
        try:
            out = reproduce_one(cfg, rec, repo_root, ledger_path)
        except Exception as exc:  # noqa: BLE001
            print(f"    !! error: {exc}")
            out = {
                "slug": slugify(rec.get("title", "")), "area": rec.get("area_code"),
                "title": rec.get("title"), "date": datetime.now().strftime("%Y-%m-%d"),
                "run_status": "harness-error", "exit_code": -3, "elapsed_s": 0,
                "error": str(exc), "produced": False, "artifacts": {},
            }
            append_jsonl(ledger_path, {
                "slug": out["slug"], "area": out["area"], "title": out["title"],
                "date": out["date"], "produced": False, "run_status": "harness-error"})
        append_jsonl(progress_path, out)
        summary["attempted"] += 1
        summary["produced"] += 1 if out.get("produced") else 0
        summary["records"].append(out)
        a = out.get("artifacts", {})
        print(f"    -> {out.get('run_status')} produced={out.get('produced')} "
              f"src={a.get('src_files', 0)} orig_figs={a.get('original_figs', 0)} "
              f"repro={a.get('reproduced_files', 0)} metrics={a.get('n_metrics', 0)} "
              f"verdict={a.get('verdict') or '-'} tests={a.get('tests', 0)} "
              f"manim={a.get('manim_files', 0)} pdf={a.get('has_summary_pdf', False)} "
              f"{out.get('elapsed_s', 0)}s")

    sdir = state / "daily"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"reproduce-{summary['date']}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] attempted={summary['attempted']} produced={summary['produced']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
