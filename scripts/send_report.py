#!/usr/bin/env python3
"""
Daily email report + unified corpus summary PDF.

On pipeline completion this script:
  1. Builds a branded, inline-styled HTML digest (Computational Observatory
     design system) from today's harvest + reproduction state: a header with the
     date and run stats (harvested / reproduced / animated), then one per-paper
     CARD for each reproduced paper -- a reproduced-figure thumbnail (embedded via
     cid:), the title + a colored area chip, the KEY FINDING (from summary.md /
     REPRODUCTION.md), a compact metrics line (verdict / figs / tests), and links
     to the private repo + local web app.
  2. Ensures every per-paper `summary.pdf` exists across ALL areas (AI/DS/ML/DL),
     generating one from summary.md / REPRODUCTION.md + results + the key figure
     when missing or stale (reportlab).
  3. MERGES every per-paper `summary.pdf` into ONE unified corpus PDF
     (state/daily/unified-summary-YYYY-MM-DD.pdf) using pypdf.
  4. Emails the digest to the address in config.json, ATTACHING both the unified
     summary PDF and the original paper PDFs processed this run.

Attachment sizing (Gmail's hard cap is ~25 MB):
  * Any original paper PDF over ~15 MB is JPEG-rasterized with PyMuPDF
    (get_pixmap ~110 DPI) into a much smaller PDF so it fits.
  * The unified summary is attached first; PDFs are then packed into emails so no
    single message exceeds ~24 MB. If the total still exceeds the budget the
    attachments are SPLIT across multiple emails -- the unified summary + the card
    digest always ride in the first email, overflow PDFs follow in continuation
    emails.

Credentials are read from config.json (Gmail SMTP App Password OR SendGrid API
key). If email is not configured (disabled / no app password / no API key), all
outputs are still written to <repo>/state/daily/ and the script exits 0 with a
clear "email pending credentials" notice -- so the pipeline NEVER breaks.

Providers: gmail_smtp (Gmail App Password) or sendgrid (API key).

Constraints honoured: Windows, Python 3.10, CPU-only. config.json is read only
(never modified -- only the orchestrator edits it). No secrets are printed. Every
risky step is wrapped so a single bad/missing artifact never crashes the run.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import smtplib
import sys
import traceback
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

# ensure the shared pdf styling module (scripts/pdf_style.py) is importable
# regardless of the current working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from secrets_helper import get_secret  # noqa: E402

# Largest total attachment payload to put on a single email (Gmail's hard limit
# is ~25 MB). The unified summary is always attached first; original PDFs are
# packed until this budget is exhausted, then SPLIT onto continuation emails.
MAX_ATTACH_BYTES = 24 * 1024 * 1024

# Any original paper PDF larger than this is JPEG-rasterized (PyMuPDF ~110 DPI)
# so it fits inside the per-email budget.
COMPRESS_THRESHOLD_BYTES = 15 * 1024 * 1024
RASTER_DPI = 110
RASTER_JPEG_QUALITY = 72

ANIM_EXTS = (".gif", ".mp4", ".webm", ".apng")
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif")


# ===========================================================================
# Computational Observatory design system (all styling is INLINED because email
# clients strip <style>/<head>). Fonts degrade gracefully to system stacks.
# ===========================================================================

C_BG = "#0A0E1A"
C_PANEL = "#121829"
C_PANEL2 = "#0E1424"       # slightly deeper for insets
C_INK = "#E8ECF6"
C_MUTED = "#8A93AD"
C_ACCENT = "#6D7BFF"
C_ACCENT2 = "#34E0E0"
C_LINE = "#232B45"         # hairline borders on dark

AREA_COLORS = {"AI": "#A78BFA", "DS": "#22D3EE", "ML": "#34D399", "DL": "#FBBF24"}
AREA_LABELS = {
    "AI": "Artificial Intelligence",
    "DS": "Data Science",
    "ML": "Machine Learning",
    "DL": "Deep Learning",
}
# folder-name -> canonical area code
_AREA_CANON = {
    "AI": "AI", "ARTIFICIAL INTELLIGENCE": "AI",
    "DS": "DS", "DATA_SCIENCE": "DS", "DATA SCIENCE": "DS",
    "ML": "ML", "MACHINE_LEARNING": "ML", "MACHINE LEARNING": "ML",
    "DL": "DL", "DEEP_LEARNING": "DL", "DEEP LEARNING": "DL",
}

FONT_TITLE = "'Spectral',Georgia,'Times New Roman',serif"
FONT_UI = "'Inter',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
FONT_MONO = "'JetBrains Mono','SFMono-Regular',Consolas,'Courier New',monospace"

# verdict keyword -> (label color, human)
_VERDICT_COLORS = {
    "full": "#34D399",
    "partial": "#FBBF24",
    "minimal": "#A78BFA",
    "infeasible": "#F87171",
    "incomplete": "#F87171",
}


def canon_area(area: str | None) -> str:
    if not area:
        return ""
    return _AREA_CANON.get(str(area).strip().upper(), str(area).strip().upper()[:2])


def area_color(area: str | None) -> str:
    return AREA_COLORS.get(canon_area(area), C_ACCENT)


# ---------------------------------------------------------------------------
# Small IO helpers
# ---------------------------------------------------------------------------

def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _xml_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt_mb(nbytes: int) -> str:
    return f"{nbytes / (1024 * 1024):.2f} MB"


# ---------------------------------------------------------------------------
# Run context: what happened this run + the whole corpus of paper dirs
# ---------------------------------------------------------------------------

def _iter_area_dirs(repo: Path, cfg: dict[str, Any]) -> list[Path]:
    """All top-level area folders that may contain per-paper reproduction dirs.

    reproduce.py writes to <repo>/<area_code>/<slug>, but we also honour the
    configured folder-name mapping so nothing is missed "across all areas".
    """
    harvest = cfg.get("harvest", {})
    names: set[str] = set()
    names.update(harvest.get("areas", {}).keys())            # AI DS ML DL
    names.update(harvest.get("area_folder_map", {}).values())  # Data_Science ...
    if not names:
        names = {"AI", "DS", "ML", "DL"}
    out = []
    for n in sorted(names):
        d = repo / n
        if d.is_dir():
            out.append(d)
    return out


def _is_paper_dir(d: Path) -> bool:
    if not d.is_dir():
        return False
    return (
        (d / "REPRODUCTION.md").exists()
        or (d / "summary.md").exists()
        or (d / "summary.pdf").exists()
        or (d / "paper.pdf").exists()
        or (d / "figures").is_dir()
    )


def corpus_paper_dirs(repo: Path, cfg: dict[str, Any]) -> list[Path]:
    """Every per-paper reproduction directory across all areas."""
    dirs: list[Path] = []
    for area in _iter_area_dirs(repo, cfg):
        try:
            children = sorted(area.iterdir())
        except Exception:  # noqa: BLE001
            continue
        for child in children:
            if _is_paper_dir(child):
                dirs.append(child)
    return dirs


def _animations_in(pdir: Path) -> list[Path]:
    """Rendered animations (manim/ preferred, then figures/) for one paper."""
    found: list[Path] = []
    for sub in ("manim", "figures"):
        d = pdir / sub
        if not d.is_dir():
            continue
        try:
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in ANIM_EXTS:
                    found.append(f)
        except Exception:  # noqa: BLE001
            continue
    return found


def collect_run(repo: Path, date: str) -> dict[str, Any]:
    """Gather everything about *this* run from harvest + reproduce state."""
    harvest = read_json(repo / "state" / "harvests" / f"harvest-{date}.json")
    repro = read_json(repo / "state" / "daily" / f"reproduce-{date}.json")

    added = [r for r in harvest.get("records", []) if r.get("status") == "added"]
    records = repro.get("records", [])
    produced = [r for r in records if r.get("produced")]

    run_dirs: list[Path] = []
    for r in records:
        pd = r.get("paper_dir")
        if pd and Path(pd).is_dir():
            run_dirs.append(Path(pd))

    original_pdfs = [d / "paper.pdf" for d in run_dirs if (d / "paper.pdf").exists()]

    animations: list[Path] = []
    for d in run_dirs:
        animations.extend(_animations_in(d))

    return {
        "harvest": harvest,
        "repro": repro,
        "added": added,
        "produced": produced,
        "attempted": repro.get("attempted", len(records)),
        "run_dirs": run_dirs,
        "original_pdfs": original_pdfs,
        "animations": animations,
    }


# ---------------------------------------------------------------------------
# Summary parsing: verdict + key finding (robust to missing/odd markdown)
# ---------------------------------------------------------------------------

def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _card_source_md(paper_dir: Path) -> Path | None:
    for name in ("summary.md", "REPRODUCTION.md"):
        p = paper_dir / name
        if p.exists():
            return p
    return None


def _strip_md(text: str) -> str:
    """Collapse a chunk of markdown into a plain one-liner."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*]+?)\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_verdict(md_text: str) -> str:
    """Extract a short reproducibility verdict, e.g. 'Partial (strong)'.

    Looks for a 'Reproducibility verdict' section and reads the first bold token
    or leading phrase; falls back to scanning for full/partial/minimal/infeasible.
    Returns "" when nothing is found. Never raises.
    """
    try:
        if not md_text:
            return ""
        lines = md_text.splitlines()
        for i, line in enumerate(lines):
            if re.search(r"reproducib\w*\s+verdict", line, re.I):
                # gather the next few non-empty lines after the heading
                tail = ""
                if ":" in line and not line.strip().startswith("#"):
                    tail = line.split(":", 1)[1]
                j = i + 1
                while j < len(lines) and (not tail.strip()):
                    if lines[j].strip():
                        tail = lines[j]
                        break
                    j += 1
                tail = _strip_md(tail)
                m = re.match(r"([A-Za-z][A-Za-z /()+-]{2,40}?)([.:]|\s—|\s-\s|$)", tail)
                if m and m.group(1).strip():
                    return m.group(1).strip().rstrip(" -")[:40]
        # fallback: first verdict keyword anywhere
        m = re.search(r"\b(full|partial|minimal|infeasible|incomplete)\b", md_text, re.I)
        if m:
            return m.group(1).capitalize()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def verdict_color(verdict: str) -> str:
    v = (verdict or "").lower()
    for key, col in _VERDICT_COLORS.items():
        if key in v:
            return col
    return C_MUTED


def key_finding(md_text: str, limit: int = 260) -> str:
    """One-to-two line key finding from the summary markdown.

    Skips the H1 title and metadata lines (**Area:** / **Slug:** / dates), returns
    the first substantive prose. Never raises.
    """
    try:
        if not md_text:
            return ""
        skip = re.compile(r"^\s*(\*\*(area|slug|generated|date)\b|#|---|===|\||>)", re.I)
        meta = re.compile(r"^\s*\*\*[A-Za-z ]+:\*\*", re.I)
        buf: list[str] = []
        for line in md_text.splitlines():
            s = line.strip()
            if not s:
                if buf:
                    break
                continue
            if skip.match(s) or meta.match(s):
                continue
            if s.lower().startswith("this summary was auto-generated"):
                # honest fallback text -- still informative, keep it
                buf.append(s)
                break
            buf.append(s)
            if sum(len(b) for b in buf) > limit:
                break
        text = _strip_md(" ".join(buf))
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        return text
    except Exception:  # noqa: BLE001
        return ""


def pick_thumb(paper_dir: Path) -> Path | None:
    """Best reproduced figure to feature as the card thumbnail, else None."""
    try:
        from pdf_style import pick_key_figure
        p = pick_key_figure(paper_dir)
        if p and Path(p).exists():
            return Path(p)
    except Exception:  # noqa: BLE001
        pass
    for sub in ("reproduced_results", "figures", "original_results"):
        d = paper_dir / sub
        if not d.is_dir():
            continue
        imgs = [p for p in sorted(d.glob("*")) if p.suffix.lower() in _IMG_EXTS]
        if imgs:
            return imgs[0]
    return None


def _count(d: Path, exts: tuple[str, ...] | None = None) -> int:
    if not d.is_dir():
        return 0
    try:
        if exts is None:
            return sum(1 for p in d.iterdir() if p.is_file())
        return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Card model: one normalized dict per reproduced paper
# ---------------------------------------------------------------------------

def _paper_site_url(repo: Path, area: str, slug: str) -> str:
    """file:// URL of the paper's page in the local web app (build_webapp.py)."""
    page = repo / "webapp" / "papers" / f"{canon_area(area)}-{slug}.html"
    return page.resolve().as_uri()


def _paper_repo_url(cfg: dict[str, Any], area: str, slug: str) -> str:
    """https URL to the paper's folder in the private GitHub repo (if configured)."""
    gh = cfg.get("github", {})
    remote = str(gh.get("remote_url") or "").strip()
    branch = str(gh.get("branch") or "main").strip()
    if not remote:
        return ""
    remote = remote.rstrip("/")
    if remote.endswith(".git"):
        remote = remote[:-4]
    return f"{remote}/tree/{branch}/{canon_area(area)}/{slug}"


def _card_from_paper_dir(pdir: Path, repo: Path, cfg: dict[str, Any],
                         rec: dict[str, Any] | None) -> dict[str, Any]:
    rec = rec or {}
    area = rec.get("area") or pdir.parent.name
    slug = rec.get("slug") or pdir.name
    src_md = _card_source_md(pdir)
    md_text = _read_text(src_md) if src_md else ""

    art = rec.get("artifacts") or {}
    figs = art.get("figures")
    if figs is None:
        figs = art.get("reproduced_imgs")
    if figs is None:
        figs = _count(pdir / "reproduced_results", _IMG_EXTS) or _count(pdir / "figures", _IMG_EXTS)
    tests = art.get("tests")
    if tests is None:
        try:
            tests = len(list((pdir / "tests").glob("test_*.py")))
        except Exception:  # noqa: BLE001
            tests = 0

    # title: H1 of summary md, else record title, else humanized slug
    title = ""
    try:
        from pdf_style import first_h1_title, humanize_title
        if src_md:
            title = first_h1_title(src_md) or ""
        title = title or rec.get("title") or humanize_title(slug)
    except Exception:  # noqa: BLE001
        title = rec.get("title") or slug.replace("-", " ").title()
    # summaries often prefix "Reproduction:"/"Reproduction summary:" -- trim it
    title = re.sub(r"^\s*reproduction(\s+summary)?\s*[:：]\s*", "", title, flags=re.I).strip()

    return {
        "area": canon_area(area),
        "slug": slug,
        "title": title or slug,
        "verdict": rec.get("verdict") or parse_verdict(md_text),
        "finding": key_finding(md_text),
        "figs": int(figs or 0),
        "tests": int(tests or 0),
        "elapsed_s": rec.get("elapsed_s"),
        "thumb": pick_thumb(pdir),
        "repo_url": rec.get("github_repo") or _paper_repo_url(cfg, area, slug),
        "site_url": _paper_site_url(repo, area, slug),
        "paper_dir": pdir,
        "original_pdf": (pdir / "paper.pdf") if (pdir / "paper.pdf").exists() else None,
        "animations": _animations_in(pdir),
    }


def collect_cards(ctx: dict[str, Any], repo: Path, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalized per-paper cards for the digest.

    Primary source is this run's *produced* reproduction records. If the run has
    no produced records (e.g. reproduce state missing/empty on this date), fall
    back to the reproduced papers already on disk so the digest is still useful --
    the pipeline runs unattended and must degrade gracefully.
    """
    cards: list[dict[str, Any]] = []
    try:
        produced = ctx.get("produced") or []
        if produced:
            for r in produced:
                pd = r.get("paper_dir")
                if not pd:
                    continue
                pdir = Path(pd)
                if not pdir.is_dir():
                    continue
                cards.append(_card_from_paper_dir(pdir, repo, cfg, r))
        if not cards:
            for pdir in corpus_paper_dirs(repo, cfg):
                # only papers that actually have a reproduction artifact
                if (pdir / "summary.pdf").exists() or (pdir / "summary.md").exists() \
                        or (pdir / "REPRODUCTION.md").exists():
                    cards.append(_card_from_paper_dir(pdir, repo, cfg, None))
    except Exception as exc:  # noqa: BLE001
        print(f"[cards] card collection degraded: {exc}")
    return cards


# ---------------------------------------------------------------------------
# HTML digest (branded, fully inline-styled)
# ---------------------------------------------------------------------------

def _chip(area: str) -> str:
    col = area_color(area)
    code = canon_area(area) or "?"
    return (f'<span style="display:inline-block;background:{col};color:#0A0E1A;'
            f'font-family:{FONT_UI};font-size:11px;font-weight:700;letter-spacing:.5px;'
            f'padding:3px 9px;border-radius:11px;">{_xml_escape(code)}</span>')


def _stat_pill(value: Any, label: str, color: str) -> str:
    return (
        f'<td style="padding:0 6px;" valign="top">'
        f'<div style="background:{C_PANEL};border:1px solid {C_LINE};border-radius:12px;'
        f'padding:12px 16px;text-align:center;">'
        f'<div style="font-family:{FONT_MONO};font-size:24px;font-weight:700;color:{color};'
        f'line-height:1;">{_xml_escape(value)}</div>'
        f'<div style="font-family:{FONT_UI};font-size:11px;color:{C_MUTED};'
        f'text-transform:uppercase;letter-spacing:1.2px;margin-top:6px;">{_xml_escape(label)}</div>'
        f'</div></td>'
    )


def _card_html(card: dict[str, Any], cid: str | None) -> str:
    acol = area_color(card["area"])
    vcol = verdict_color(card["verdict"])
    verdict = card["verdict"] or "n/a"

    # thumbnail cell (fixed-width) -- embedded via cid, degrades to a placeholder
    if cid:
        thumb = (
            f'<img src="cid:{cid}" width="180" alt="reproduced figure" '
            f'style="display:block;width:180px;max-width:180px;height:auto;'
            f'border-radius:8px;border:1px solid {C_LINE};background:{C_PANEL2};" />'
        )
    else:
        thumb = (
            f'<div style="width:180px;height:120px;border-radius:8px;border:1px solid {C_LINE};'
            f'background:{C_PANEL2};font-family:{FONT_UI};font-size:11px;color:{C_MUTED};'
            f'text-align:center;line-height:120px;">no figure</div>'
        )

    finding = card["finding"] or "Reproduction artifacts available; see the summary PDF."

    metrics = (
        f'<span style="color:{vcol};font-weight:700;">◆ {_xml_escape(verdict)}</span>'
        f'<span style="color:{C_LINE};"> &nbsp;|&nbsp; </span>'
        f'<span style="color:{C_INK};">{card["figs"]}</span>'
        f'<span style="color:{C_MUTED};"> figs</span>'
        f'<span style="color:{C_LINE};"> &nbsp;|&nbsp; </span>'
        f'<span style="color:{C_INK};">{card["tests"]}</span>'
        f'<span style="color:{C_MUTED};"> tests</span>'
    )
    if card["animations"]:
        metrics += (f'<span style="color:{C_LINE};"> &nbsp;|&nbsp; </span>'
                    f'<span style="color:{C_ACCENT2};">▷ {len(card["animations"])} anim</span>')

    links = []
    if card.get("repo_url"):
        links.append(
            f'<a href="{_xml_escape(card["repo_url"])}" '
            f'style="color:{C_ACCENT};text-decoration:none;font-weight:600;">repo ↗</a>')
    if card.get("site_url"):
        links.append(
            f'<a href="{_xml_escape(card["site_url"])}" '
            f'style="color:{C_ACCENT2};text-decoration:none;font-weight:600;">local site ↗</a>')
    links_html = (f'<span style="color:{C_LINE};"> &nbsp;·&nbsp; </span>'.join(links)) or \
        f'<span style="color:{C_MUTED};">artifacts in repo</span>'

    return f"""
  <tr><td style="padding:8px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
      style="background:{C_PANEL};border:1px solid {C_LINE};border-left:4px solid {acol};
      border-radius:14px;">
      <tr>
        <td width="196" valign="top" style="padding:16px 8px 16px 16px;">{thumb}</td>
        <td valign="top" style="padding:16px 16px 16px 8px;">
          <div style="margin:0 0 8px 0;">{_chip(card["area"])}
            <span style="font-family:{FONT_UI};font-size:11px;color:{C_MUTED};margin-left:6px;">
            {_xml_escape(AREA_LABELS.get(canon_area(card["area"]), ""))}</span>
          </div>
          <div style="font-family:{FONT_TITLE};font-size:18px;font-weight:600;color:{C_INK};
            line-height:1.28;margin:0 0 8px 0;">{_xml_escape(card["title"])}</div>
          <div style="font-family:{FONT_UI};font-size:13px;color:{C_MUTED};line-height:1.5;
            margin:0 0 12px 0;">{_xml_escape(finding)}</div>
          <div style="font-family:{FONT_MONO};font-size:12px;margin:0 0 8px 0;">{metrics}</div>
          <div style="font-family:{FONT_UI};font-size:12px;">{links_html}</div>
        </td>
      </tr>
    </table>
  </td></tr>"""


def build_html(ctx: dict[str, Any], date: str) -> tuple[str, list[Path]]:
    """Return (html, inline_figure_paths) for the primary digest email.

    Header (date + run stats: harvested / reproduced / animated) then a per-paper
    card grid. ``ctx['cards']`` must already be populated by :func:`collect_cards`.
    """
    cards = ctx.get("cards") or []
    added = ctx.get("added") or []
    n_anim = sum(len(c.get("animations") or []) for c in cards)

    inline_figs: list[Path] = []
    card_blocks: list[str] = []
    for c in cards:
        cid = None
        thumb = c.get("thumb")
        if thumb and Path(thumb).exists():
            cid = f"thumb{len(inline_figs)}"
            inline_figs.append(Path(thumb))
        card_blocks.append(_card_html(c, cid))

    if not card_blocks:
        card_blocks.append(
            f'<tr><td style="font-family:{FONT_UI};font-size:14px;color:{C_MUTED};'
            f'padding:24px;text-align:center;background:{C_PANEL};border:1px solid {C_LINE};'
            f'border-radius:14px;">No reproductions to report this run.</td></tr>')

    # header stats row
    stats = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr>'
        + _stat_pill(len(added), "harvested", C_ACCENT2)
        + _stat_pill(len(cards), "reproduced", C_ACCENT)
        + _stat_pill(n_anim, "animated", AREA_COLORS["DL"])
        + '</tr></table>'
    )

    preheader = (f"{len(cards)} reproduced · {len(added)} harvested · {n_anim} animated "
                 f"— {date}")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Spectral:wght@500;600&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
</head>
<body style="margin:0;padding:0;background:{C_BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_xml_escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{C_BG};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" style="width:680px;max-width:100%;">

  <!-- header -->
  <tr><td style="padding:0 0 18px 0;">
    <div style="font-family:{FONT_UI};font-size:12px;font-weight:700;letter-spacing:3px;
      text-transform:uppercase;color:{C_ACCENT};margin-bottom:6px;">Computational Observatory</div>
    <div style="font-family:{FONT_TITLE};font-size:30px;font-weight:600;color:{C_INK};
      line-height:1.15;">Agentic AI Researcher</div>
    <div style="font-family:{FONT_MONO};font-size:13px;color:{C_MUTED};margin-top:6px;">
      daily digest &nbsp;·&nbsp; {_xml_escape(date)}</div>
    <div style="height:3px;width:100%;margin-top:14px;border-radius:2px;
      background:linear-gradient(90deg,{C_ACCENT},{C_ACCENT2});
      background-color:{C_ACCENT};font-size:0;line-height:0;">&nbsp;</div>
  </td></tr>

  <!-- run stats -->
  <tr><td style="padding:6px 0 14px 0;">{stats}</td></tr>

  <!-- section label -->
  <tr><td style="padding:8px 0 2px 0;">
    <div style="font-family:{FONT_UI};font-size:13px;font-weight:700;letter-spacing:2px;
      text-transform:uppercase;color:{C_MUTED};">Reproduced papers</div>
  </td></tr>

  {''.join(card_blocks)}

  <!-- footer -->
  <tr><td style="padding:22px 4px 8px 4px;">
    <div style="height:1px;background:{C_LINE};font-size:0;line-height:0;">&nbsp;</div>
    <div style="font-family:{FONT_UI};font-size:12px;color:{C_MUTED};line-height:1.6;margin-top:14px;">
      Attached: the unified corpus summary PDF + the original paper PDFs processed this run
      (papers over ~15&nbsp;MB are JPEG-rasterized to fit; overflow is split across follow-up emails).
      Browse the local web app for full figures, source &amp; tests.
    </div>
    <div style="font-family:{FONT_MONO};font-size:11px;color:{C_LINE};margin-top:10px;">
      AI_DS_ML_DL_Researcher</div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""
    return html, inline_figs


def build_continuation_html(date: str, part: int, total: int, attachments: list[Path]) -> str:
    """Lightweight branded body for a split/overflow email (part k of n)."""
    rows = "".join(
        f'<li style="margin:2px 0;">{_xml_escape(_attach_display_name(p))}</li>'
        for p in attachments)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:{C_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{C_BG};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" style="width:680px;max-width:100%;">
  <tr><td>
    <div style="font-family:{FONT_UI};font-size:12px;font-weight:700;letter-spacing:3px;
      text-transform:uppercase;color:{C_ACCENT};margin-bottom:6px;">Computational Observatory</div>
    <div style="font-family:{FONT_TITLE};font-size:24px;font-weight:600;color:{C_INK};">
      Attachments — part {part} of {total}</div>
    <div style="font-family:{FONT_MONO};font-size:13px;color:{C_MUTED};margin-top:6px;">{_xml_escape(date)}</div>
    <div style="height:3px;width:100%;margin:14px 0;border-radius:2px;
      background:linear-gradient(90deg,{C_ACCENT},{C_ACCENT2});background-color:{C_ACCENT};
      font-size:0;line-height:0;">&nbsp;</div>
    <div style="font-family:{FONT_UI};font-size:13px;color:{C_MUTED};line-height:1.6;">
      These paper PDFs did not fit the 25&nbsp;MB limit of the first email and are delivered here:
    </div>
    <ul style="font-family:{FONT_MONO};font-size:12px;color:{C_INK};line-height:1.6;">{rows}</ul>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


# ---------------------------------------------------------------------------
# Per-paper summary.pdf generation (reportlab) + unified merge (pypdf)
# ---------------------------------------------------------------------------

def _summary_source_md(paper_dir: Path) -> Path | None:
    """The markdown the summary PDF is rendered from (summary.md preferred)."""
    for name in ("summary.md", "REPRODUCTION.md"):
        p = paper_dir / name
        if p.exists():
            return p
    return None


def _needs_summary(paper_dir: Path) -> bool:
    summ = paper_dir / "summary.pdf"
    if not summ.exists():
        return True
    src = _summary_source_md(paper_dir)
    if src and src.stat().st_mtime > summ.stat().st_mtime:
        return True
    return False


def generate_summary_pdf(paper_dir: Path, area: str) -> Path | None:
    """Build a branded per-paper summary.pdf from the reproduction artifacts.

    Delegates all styling to the shared :mod:`pdf_style` module (identical look to
    the per-paper PDFs written by reproduce.py). Renders summary.md (preferred) or
    REPRODUCTION.md. Returns the path on success, else None. Never raises.
    """
    try:
        from pdf_style import build_summary_pdf, first_h1_title, humanize_title, \
            pick_key_figure
    except Exception as exc:  # noqa: BLE001
        print(f"[summary] pdf_style/reportlab unavailable ({exc}); cannot generate summaries.")
        return None

    src_md = _summary_source_md(paper_dir)
    key_fig = pick_key_figure(paper_dir)
    if not (src_md or key_fig or (paper_dir / "paper.pdf").exists()):
        return None  # nothing worth summarising

    out = paper_dir / "summary.pdf"
    meta = {
        "title": (first_h1_title(src_md) if src_md else None)
        or humanize_title(paper_dir.name),
        "area": area,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "key_figure_path": key_fig,
    }
    try:
        # if there is no source markdown, still emit a minimal branded cover page
        source = src_md if src_md else out.with_suffix(".missing.md")
        ok = build_summary_pdf(source, out, meta)
        return out if ok else None
    except Exception as exc:  # noqa: BLE001
        print(f"[summary] build failed for {paper_dir.name}: {exc}")
        return None


def ensure_all_summaries(repo: Path, cfg: dict[str, Any]) -> list[Path]:
    """Ensure every per-paper summary.pdf exists across all areas; return them."""
    summaries: list[Path] = []
    for pdir in corpus_paper_dirs(repo, cfg):
        area = pdir.parent.name
        try:
            if _needs_summary(pdir):
                generate_summary_pdf(pdir, area)
        except Exception as exc:  # noqa: BLE001
            print(f"[summary] skip {pdir.name}: {exc}")
        summ = pdir / "summary.pdf"
        if summ.exists():
            summaries.append(summ)
    return summaries


def merge_summaries(summaries: list[Path], out_pdf: Path) -> Path | None:
    """Merge every per-paper summary.pdf into ONE unified PDF via pypdf."""
    if not summaries:
        print("[unified] no per-paper summaries found; nothing to merge.")
        return None
    try:
        from pypdf import PdfWriter
    except Exception as exc:  # noqa: BLE001
        print(f"[unified] pypdf unavailable ({exc}); cannot merge.")
        return None

    writer = PdfWriter()
    merged = 0
    for s in summaries:
        try:
            writer.append(str(s))
            merged += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[unified] skip unreadable summary {s}: {exc}")
    if merged == 0:
        return None
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_pdf.open("wb") as f:
            writer.write(f)
    finally:
        writer.close()
    print(f"[unified] merged {merged} summary PDF(s) -> {out_pdf}")
    return out_pdf


# ---------------------------------------------------------------------------
# PDF compression (JPEG-rasterize oversized papers via PyMuPDF ~110 DPI)
# ---------------------------------------------------------------------------

def compress_pdf(src: Path, out_dir: Path, dpi: int = RASTER_DPI) -> Path | None:
    """Rasterize every page of ``src`` to JPEG at ~``dpi`` and repackage as a PDF.

    Writes to ``out_dir`` (never touches the original). Returns the new path, or
    None on any failure. Never raises.
    """
    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # noqa: BLE001
        print(f"[compress] PyMuPDF unavailable ({exc}); leaving {src.name} as-is.")
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{src.parent.name}.pdf"
        doc = fitz.open(str(src))
        new = fitz.open()
        try:
            for page in doc:
                pix = page.get_pixmap(dpi=dpi)
                jpg = pix.tobytes("jpeg", jpg_quality=RASTER_JPEG_QUALITY)
                rect = page.rect
                npage = new.new_page(width=rect.width, height=rect.height)
                npage.insert_image(npage.rect, stream=jpg)
            new.save(str(out), deflate=True, garbage=4)
        finally:
            new.close()
            doc.close()
        return out if out.exists() else None
    except Exception as exc:  # noqa: BLE001
        print(f"[compress] failed for {src.name}: {exc}")
        return None


def prepare_originals(originals: list[Path], out_dir: Path,
                      threshold: int = COMPRESS_THRESHOLD_BYTES) -> tuple[list[Path], list[dict[str, Any]]]:
    """Return (usable_paths, decisions). Oversized PDFs are rasterized; the
    smaller of (compressed, original) is used. Decisions describe every choice."""
    usable: list[Path] = []
    decisions: list[dict[str, Any]] = []
    for p in originals:
        if not p.exists():
            continue
        orig_sz = p.stat().st_size
        name = _attach_display_name(p)
        if orig_sz <= threshold:
            usable.append(p)
            decisions.append({"name": name, "action": "attach-as-is",
                              "orig_bytes": orig_sz, "final_bytes": orig_sz,
                              "path": p})
            continue
        comp = compress_pdf(p, out_dir)
        if comp and comp.exists() and comp.stat().st_size < orig_sz:
            csz = comp.stat().st_size
            usable.append(comp)
            decisions.append({"name": name, "action": f"rasterized@{RASTER_DPI}dpi",
                              "orig_bytes": orig_sz, "final_bytes": csz, "path": comp})
        else:
            usable.append(p)
            decisions.append({"name": name, "action": "over-threshold-compress-failed",
                              "orig_bytes": orig_sz, "final_bytes": orig_sz, "path": p})
    return usable, decisions


# ---------------------------------------------------------------------------
# Attachment planning (compress + split across emails under the budget)
# ---------------------------------------------------------------------------

def select_attachments(unified: Path | None, originals: list[Path]) -> tuple[list[Path], list[Path]]:
    """Back-compat single-email selector honouring MAX_ATTACH_BYTES (unified first)."""
    attach: list[Path] = []
    skipped: list[Path] = []
    total = 0
    if unified and unified.exists():
        attach.append(unified)
        total += unified.stat().st_size
    for p in originals:
        if not p.exists():
            continue
        sz = p.stat().st_size
        if total + sz > MAX_ATTACH_BYTES:
            skipped.append(p)
            continue
        attach.append(p)
        total += sz
    return attach, skipped


def plan_emails(unified: Path | None, prepared: list[Path],
                budget: int = MAX_ATTACH_BYTES) -> list[dict[str, Any]]:
    """Pack the unified summary + prepared PDFs into >=1 email batches under
    ``budget`` bytes each. The unified summary always rides in the first batch.

    Returns a list of dicts: {"attachments":[Path...], "bytes":int,
    "oversize":[Path...]}. A single PDF that still exceeds the budget after
    compression is placed alone and flagged in ``oversize``.
    """
    batches: list[dict[str, Any]] = []
    cur: list[Path] = []
    cur_bytes = 0
    oversize: list[Path] = []

    if unified and unified.exists():
        cur.append(unified)
        cur_bytes += unified.stat().st_size

    for p in prepared:
        if not p.exists():
            continue
        sz = p.stat().st_size
        if cur and cur_bytes + sz > budget:
            batches.append({"attachments": cur, "bytes": cur_bytes, "oversize": oversize})
            cur, cur_bytes, oversize = [], 0, []
        cur.append(p)
        cur_bytes += sz
        if sz > budget:
            oversize.append(p)
    if cur or not batches:
        batches.append({"attachments": cur, "bytes": cur_bytes, "oversize": oversize})
    return batches


# ---------------------------------------------------------------------------
# Email sending (Gmail SMTP or SendGrid)
# ---------------------------------------------------------------------------

def _attach_display_name(p: Path) -> str:
    """A readable attachment filename (paper.pdf lives per-slug dir)."""
    if p.name == "paper.pdf":
        return f"{p.parent.name}.pdf"
    return p.name


def _img_subtype(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return "jpeg"
    if ext == ".gif":
        return "gif"
    return "png"


def has_credentials(cfg: dict[str, Any]) -> bool:
    e = cfg.get("email", {})
    if not e.get("enabled"):
        return False
    if e.get("provider") == "sendgrid":
        return bool(e.get("sendgrid_api_key"))
    # gmail/smtp: the password may come from Secret Manager (gmail-app-password),
    # the GMAIL_APP_PASSWORD env var, or the config.json fallback. Mirror send().
    try:
        return bool(get_secret("gmail-app-password", env="GMAIL_APP_PASSWORD",
                               value_fallback=e.get("app_password")))
    except Exception:  # noqa: BLE001
        return bool(e.get("app_password"))


def send(cfg: dict[str, Any], html: str, figs: list[Path], attachments: list[Path],
         date: str, part: int | None = None, total: int | None = None) -> str:
    e = cfg["email"]
    suffix = f" (part {part}/{total})" if part and total and total > 1 else ""
    subject = f"[Researcher] {date}: daily progress + unified summary{suffix}"

    if e.get("provider") == "sendgrid" and e.get("sendgrid_api_key"):
        # stale path (key blanked in config); kept functional. Routed through
        # the shared polite client so it can never retry-hammer: max 4
        # attempts, Retry-After honored, then ProviderBlocked ends the send.
        import polite_http  # scripts/ is on sys.path (NETWORK_ETIQUETTE.md)
        atts = []
        for i, f in enumerate(figs):
            atts.append({"content": base64.b64encode(f.read_bytes()).decode(),
                         "type": f"image/{_img_subtype(f)}", "filename": f.name,
                         "disposition": "inline", "content_id": f"thumb{i}"})
        for f in attachments:
            atts.append({"content": base64.b64encode(f.read_bytes()).decode(),
                         "type": "application/pdf", "filename": _attach_display_name(f),
                         "disposition": "attachment"})
        payload = {
            "personalizations": [{"to": [{"email": e["to_addr"]}]}],
            "from": {"email": e["from_addr"]},
            "subject": subject,
            "content": [{"type": "text/html", "value": html}],
            "attachments": atts,
        }
        r = polite_http.post("https://api.sendgrid.com/v3/mail/send",
                             headers={"Authorization": f"Bearer {e['sendgrid_api_key']}"},
                             json=payload, timeout=60, ua_suffix="+send-report")
        r.raise_for_status()
        return "sent via sendgrid"

    # gmail / generic SMTP. Prefer Secret Manager (secret 'gmail-app-password')
    # with a safe fallback chain: GMAIL_APP_PASSWORD env -> Secret Manager ->
    # gcloud CLI -> the app_password value already parsed from config.json.
    try:
        app_password = get_secret("gmail-app-password", env="GMAIL_APP_PASSWORD",
                                  value_fallback=e.get("app_password"))
    except Exception:  # noqa: BLE001
        app_password = e.get("app_password")
    if not app_password:
        return "no-credentials"
    outer = MIMEMultipart("mixed")
    outer["Subject"] = subject
    outer["From"] = e["from_addr"]
    outer["To"] = e["to_addr"]

    related = MIMEMultipart("related")
    related.attach(MIMEText(html, "html"))
    for i, f in enumerate(figs):
        img = MIMEImage(f.read_bytes(), _subtype=_img_subtype(f))
        img.add_header("Content-ID", f"<thumb{i}>")
        img.add_header("Content-Disposition", "inline", filename=f.name)
        related.attach(img)
    outer.attach(related)

    for f in attachments:
        part_att = MIMEApplication(f.read_bytes(), _subtype="pdf")
        part_att.add_header("Content-Disposition", "attachment", filename=_attach_display_name(f))
        outer.attach(part_att)

    with smtplib.SMTP(e["smtp_host"], e["smtp_port"], timeout=60) as s:
        s.starttls()
        s.login(e["username"], app_password)
        s.sendmail(e["from_addr"], [e["to_addr"]], outer.as_string())
    return "sent via gmail_smtp"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _print_plan(cards: list[dict[str, Any]], decisions: list[dict[str, Any]],
                batches: list[dict[str, Any]], unified: Path | None) -> None:
    print(f"[plan] cards (reproduced papers): {len(cards)}")
    print("[plan] compression decisions:")
    if not decisions:
        print("    (no original paper PDFs this run)")
    for d in decisions:
        print(f"    - {d['name']}: {d['action']} "
              f"({_fmt_mb(d['orig_bytes'])} -> {_fmt_mb(d['final_bytes'])})")
    print(f"[plan] emails: {len(batches)} "
          f"(unified summary {'attached' if unified else 'absent'})")
    for i, b in enumerate(batches, 1):
        names = ", ".join(_attach_display_name(p) for p in b["attachments"]) or "(none)"
        over = f"  OVERSIZE:{len(b['oversize'])}" if b.get("oversize") else ""
        print(f"    email {i}/{len(batches)}: {_fmt_mb(b['bytes'])} "
              f"in {len(b['attachments'])} file(s){over}  [{names}]")


# ---------------------------------------------------------------------------
# new-work dedup gate: only email papers not already delivered in a prior run
# ---------------------------------------------------------------------------
def _emailed_ledger_path(repo: Path) -> Path:
    return repo / "state" / "emailed_ledger.json"


def load_emailed_slugs(repo: Path) -> set[str]:
    """Set of paper slugs (paper-dir names) already included in a sent email."""
    p = _emailed_ledger_path(repo)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("slugs", []))
    except Exception:  # noqa: BLE001
        return set()


def record_emailed_slugs(repo: Path, slugs: set[str]) -> None:
    """Merge *slugs* into the emailed ledger (append-only union)."""
    p = _emailed_ledger_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = load_emailed_slugs(repo)
    merged = sorted(existing | slugs)
    tmp = {"slugs": merged, "updated": datetime.now().isoformat(timespec="seconds")}
    p.write_text(json.dumps(tmp, indent=2), encoding="utf-8")


def _slug_of(path: Path) -> str:
    """The paper slug for any artifact inside a paper dir (…/<slug>/summary.pdf,
    …/<slug>/paper.pdf) is that directory's name."""
    return path.parent.name


# ---------------------------------------------------------------------------
# quiet hours: never transmit at night; hold and release at send_from (10:00)
# ---------------------------------------------------------------------------
def _parse_hhmm(s: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(s).split(":")
        return int(hh), int(mm)
    except Exception:  # noqa: BLE001
        return default


def _quiet_cfg(cfg: dict[str, Any]) -> tuple[bool, tuple[int, int], tuple[int, int]]:
    """(enabled, send_from HH:MM, hold_start HH:MM). Emails transmit only in the
    window [send_from, hold_start); outside it they are held. send_from doubles as
    the release time for held mail (default 10:00; hold_start default 23:00)."""
    q = (cfg.get("email", {}) or {}).get("quiet_hours", {}) or {}
    enabled = bool(q.get("enabled", False))
    return (enabled,
            _parse_hhmm(q.get("send_from", "10:00"), (10, 0)),
            _parse_hhmm(q.get("hold_start", "23:00"), (23, 0)))


def _send_allowed_now(cfg: dict[str, Any], now: datetime) -> bool:
    from datetime import time as _time
    enabled, (fh, fm), (hh, hm) = _quiet_cfg(cfg)
    if not enabled:
        return True
    t = now.time()
    start, end = _time(fh, fm), _time(hh, hm)
    if start <= end:                      # normal same-day window, e.g. 10:00–23:00
        return start <= t < end
    return t >= start or t < end          # wrap-around window (unusual)


def _next_release(cfg: dict[str, Any], now: datetime) -> datetime:
    """Next occurrence of send_from at/after now (today if still ahead, else tomorrow)."""
    from datetime import timedelta
    _, (fh, fm), _ = _quiet_cfg(cfg)
    cand = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def _pending_path(repo: Path) -> Path:
    return repo / "state" / "email_pending.json"


def write_email_pending(repo: Path, release_at: datetime, now: datetime, n_batches: int) -> None:
    p = _pending_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "held_at": now.isoformat(timespec="seconds"),
        "release_at": release_at.isoformat(timespec="seconds"),
        "batches_held": n_batches,
    }, indent=2), encoding="utf-8")


def clear_email_pending(repo: Path) -> None:
    p = _pending_path(repo)
    if p.exists():
        try:
            p.unlink()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily digest + unified corpus summary PDF emailer.")
    here = Path(__file__).resolve().parent
    ap.add_argument("--config", type=Path, default=here.parent / "config.json")
    ap.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--dry-run", action="store_true",
                    help="build the digest + attachment plan and write outputs, "
                         "but do NOT send any email (prints the plan).")
    ap.add_argument("--all", action="store_true",
                    help="bypass the new-work dedup gate and (re)send the WHOLE corpus, "
                         "even papers already emailed before.")
    ap.add_argument("--ignore-quiet-hours", action="store_true",
                    help="send now even inside the email.quiet_hours window (manual override).")
    args = ap.parse_args()

    if not args.config.exists():
        print(f"[fatal] config not found: {args.config}")
        return 2

    cfg = load(args.config)  # read-only; never modified here
    import pipeline_paths  # scripts/ is on sys.path
    repo = pipeline_paths.repo_root(cfg)
    date = args.date

    daily = repo / "state" / "daily"
    daily.mkdir(parents=True, exist_ok=True)

    # ---- gather run context + cards + HTML digest ----------------------
    try:
        ctx = collect_run(repo, date)
        ctx["cards"] = collect_cards(ctx, repo, cfg)
        html, figs = build_html(ctx, date)
    except Exception as exc:  # noqa: BLE001
        print(f"[report] digest build failed: {exc}")
        traceback.print_exc()
        ctx = {"cards": [], "original_pdfs": [], "animations": [], "added": [], "produced": []}
        html, figs = f"<html><body><p>Report build error: {_xml_escape(str(exc))}</p></body></html>", []

    try:
        (daily / f"report-{date}.html").write_text(html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[report] could not write report html: {exc}")

    # ---- new-work dedup gate -------------------------------------------
    #  By default only email papers whose reproduction has not been delivered in a
    #  prior run, so the ~6h cycle does not re-send the whole corpus every time.
    #  --all (or email.only_new=false) restores the send-everything behavior.
    only_new = bool(cfg.get("email", {}).get("only_new", True)) and not args.all
    already_emailed = load_emailed_slugs(repo) if only_new else set()

    # ---- ensure per-paper summaries + merge into unified PDF -----------
    unified: Path | None = None
    new_slugs: set[str] = set()
    try:
        summaries = ensure_all_summaries(repo, cfg)
        if only_new:
            fresh = [s for s in summaries if _slug_of(s) not in already_emailed]
            new_slugs = {_slug_of(s) for s in fresh}
            skipped = len(summaries) - len(fresh)
            print(f"[gate] new-work-only: {len(fresh)} new paper(s) to email, "
                  f"{skipped} already delivered (ledger: {len(already_emailed)} slugs).")
            if not fresh:
                print("[gate] nothing new to email; outputs saved under "
                      f"{daily}. Use --all to resend the whole corpus.")
                clear_email_pending(repo)  # nothing to hold/release
                return 0
            summaries = fresh
        else:
            new_slugs = {_slug_of(s) for s in summaries}
        unified = merge_summaries(summaries, daily / f"unified-summary-{date}.pdf")
    except Exception as exc:  # noqa: BLE001
        print(f"[unified] summary/merge failed (continuing): {exc}")
        traceback.print_exc()

    # ---- originals this run: compress oversized, then plan email split -
    cards = ctx.get("cards") or []
    originals: list[Path] = []
    seen: set[str] = set()
    for c in cards:
        op = c.get("original_pdf")
        if op and Path(op).exists() and str(op) not in seen:
            originals.append(Path(op))
            seen.add(str(op))
    # include any run originals not represented by a card (robustness)
    for op in ctx.get("original_pdfs", []):
        if op and Path(op).exists() and str(op) not in seen:
            originals.append(Path(op))
            seen.add(str(op))
    # under the new-work gate, only attach originals for the newly-emailed papers
    if only_new:
        originals = [p for p in originals if _slug_of(p) in new_slugs]

    try:
        prepared, decisions = prepare_originals(originals, daily / "compressed")
    except Exception as exc:  # noqa: BLE001
        print(f"[compress] preparation failed (attaching raw): {exc}")
        prepared, decisions = originals, []

    batches = plan_emails(unified, prepared)

    print(f"[outputs] html={daily / f'report-{date}.html'}")
    if unified:
        print(f"[outputs] unified_summary={unified}")
    print(f"[outputs] reproduced_cards={len(cards)} originals_this_run={len(originals)}")
    _print_plan(cards, decisions, batches, unified)

    # ---- dry run: stop before sending ----------------------------------
    if args.dry_run:
        print(f"[dry-run] no email sent; all outputs saved under {daily}.")
        return 0

    # ---- email (or save-and-exit if creds absent) ----------------------
    if not has_credentials(cfg):
        print("[email] pending credentials (set email.enabled + app_password/sendgrid_api_key in config.json); "
              f"all outputs saved under {daily}. Pipeline unaffected.")
        return 0

    # ---- quiet hours: never transmit at night; hold and release later --
    #  Papers stay un-recorded (still "new"), so the session-runner re-runs this
    #  script at the release time (send_from, default 10:00) and it sends then.
    now = datetime.now()
    if not args.ignore_quiet_hours and not _send_allowed_now(cfg, now):
        rel = _next_release(cfg, now)
        write_email_pending(repo, rel, now, len(batches))
        print(f"[quiet-hours] holding {len(batches)} email batch(es) until "
              f"{rel:%Y-%m-%d %H:%M}; {len(new_slugs)} new paper(s) remain queued "
              f"(not recorded as emailed). Outputs saved under {daily}.")
        return 0

    total = len(batches)
    sent = 0
    for i, b in enumerate(batches, 1):
        body = html if i == 1 else build_continuation_html(date, i, total, b["attachments"])
        body_figs = figs if i == 1 else []
        try:
            status = send(cfg, body, body_figs, b["attachments"], date,
                          part=i, total=total)
        except Exception as exc:  # noqa: BLE001
            print(f"[email] send failed for part {i}/{total}: {exc}; outputs saved under {daily}.")
            continue
        if status == "no-credentials":
            print(f"[email] pending credentials; outputs saved under {daily}.")
            return 0
        sent += 1
        print(f"[email] {status} -> {cfg['email']['to_addr']} "
              f"(part {i}/{total}, {len(b['attachments'])} attachment(s))")

    if sent == 0:
        print(f"[email] nothing sent; outputs saved under {daily}.")
        return 0

    # record the delivered papers so the next cycle skips them (gate is by paper,
    # so a partial multi-batch send still won't re-deliver what did go out).
    if new_slugs:
        record_emailed_slugs(repo, new_slugs)
        print(f"[gate] recorded {len(new_slugs)} paper(s) as emailed "
              f"-> {_emailed_ledger_path(repo)}")
    clear_email_pending(repo)  # released whatever was held
    return 0


if __name__ == "__main__":
    sys.exit(main())
