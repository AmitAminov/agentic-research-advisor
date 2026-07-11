#!/usr/bin/env python3
"""
Shared, branded PDF styling for the AI-DS-ML-DL Researcher.

Both PDF generators in this project render a paper's reproduction ``summary.md``
(or ``REPRODUCTION.md``) into a polished, scholarly ``summary.pdf``:

  * ``reproduce.py``  -> ``markdown_to_pdf()``  (per-paper, at reproduction time)
  * ``send_report.py`` -> ``generate_summary_pdf()`` (corpus sweep + unified merge)

To keep every summary PDF visually identical and on-brand, BOTH delegate to the
single entrypoint here:

    build_summary_pdf(summary_md_path, out_pdf_path, meta) -> bool

``meta`` is a best-effort dict; every key is optional and a missing/broken value
(figure, title, area, date, repo_url) must NEVER crash the build. The function
always tries to emit a valid PDF.

Design: a light, print-friendly scholarly theme.
  * Accent #6D7BFF (indigo), secondary #34E0E0 (cyan).
  * Area chips (print-legible): AI #7C3AED, DS #0891B2, ML #059669, DL #D97706.
  * Fonts: reportlab built-ins only (no external TTFs) - Times-Roman family for
    the body/title (scholarly serif), Helvetica for labels/captions, Courier for
    code/metrics.
  * A cover page (title, area chip, date, wordmark, optional key figure), then a
    styled body with accent headings, justified text, bullet/number lists,
    monospace code boxes, and tinted-header results tables. Every body page gets
    a hairline footer ("AI-DS-ML-DL Researcher" left, "page N" right).
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# Brand palette + typography
# ---------------------------------------------------------------------------
ACCENT = "#6D7BFF"        # indigo
ACCENT_DARK = "#4C5AE0"   # heading ink (a touch darker for legibility)
SECONDARY = "#34E0E0"     # cyan
ACCENT_TINT = "#EEEFFF"   # light indigo wash (table header / code hints)
INK = "#1A1A22"           # body text
MUTED = "#6B7280"         # captions / footer
HAIRLINE = "#D9DBE6"      # thin rules / table grid
CODE_BG = "#F4F5F8"       # code block background

# Area code -> (chip color, human label). Accepts codes or folder names.
_AREA_COLORS = {"AI": "#7C3AED", "DS": "#0891B2", "ML": "#059669", "DL": "#D97706"}
_AREA_LABEL = {
    "AI": "Artificial Intelligence",
    "DS": "Data Science",
    "ML": "Machine Learning",
    "DL": "Deep Learning",
}
_AREA_CANON = {
    "AI": "AI", "ARTIFICIAL INTELLIGENCE": "AI",
    "DS": "DS", "DATA_SCIENCE": "DS", "DATA SCIENCE": "DS",
    "ML": "ML", "MACHINE_LEARNING": "ML", "MACHINE LEARNING": "ML",
    "DL": "DL", "DEEP_LEARNING": "DL", "DEEP LEARNING": "DL",
}

WORDMARK = "Reproduced by the AI·DS·ML·DL Researcher"
FOOTER_LEFT = "AI·DS·ML·DL Researcher"

SERIF = "Times-Roman"
SERIF_B = "Times-Bold"
SERIF_I = "Times-Italic"
SERIF_BI = "Times-BoldItalic"
SANS = "Helvetica"
SANS_B = "Helvetica-Bold"
MONO = "Courier"

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif")


# ---------------------------------------------------------------------------
# Small, dependency-free helpers (safe to import even without reportlab)
# ---------------------------------------------------------------------------

def canon_area(area: str | None) -> str | None:
    if not area:
        return None
    return _AREA_CANON.get(str(area).strip().upper())


def area_color(area: str | None) -> str:
    return _AREA_COLORS.get(canon_area(area) or "", ACCENT)


def area_label(area: str | None) -> str:
    code = canon_area(area)
    if not code:
        return str(area or "").strip()
    return f"{code} · {_AREA_LABEL[code]}"


def humanize_title(slug: str) -> str:
    return (slug or "paper").replace("-", " ").replace("_", " ").strip().title() or "paper"


def first_h1_title(md_path: Path) -> str | None:
    """First markdown ``# H1`` heading of a file, with inline markup stripped."""
    try:
        for line in md_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("# "):
                t = s[2:].strip()
                t = re.sub(r"[*`_]+", "", t).strip()
                return t or None
    except Exception:  # noqa: BLE001
        return None
    return None


def pick_key_figure(paper_dir: Path) -> str | None:
    """Best reproduced figure to feature on the cover, else None.

    Preference order: reproduced_results/ (the reproduction's own output), then
    figures/, then original_results/. Skips tiny/decorative extraction artifacts
    where possible by favouring 'main'/'comparison'/'result'-named files.
    """
    if not paper_dir or not paper_dir.is_dir():
        return None
    for sub in ("reproduced_results", "figures", "original_results"):
        d = paper_dir / sub
        if not d.is_dir():
            continue
        imgs = [p for p in sorted(d.glob("*")) if p.suffix.lower() in _IMG_EXTS]
        if not imgs:
            continue
        pref = [p for p in imgs if re.search(r"main|compar|result|repro|final|fig",
                                              p.name, re.I)]
        return str((pref or imgs)[0])
    return None


def _md_inline(text: str) -> str:
    """Escape XML, then re-apply a tiny subset of inline markdown as RL markup."""
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+?)`", r'<font face="%s">\1</font>' % MONO, text)
    # bare markdown links [txt](url) -> txt (url); keep it plain + robust
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    return text


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def _styles():
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle

    body = ParagraphStyle(
        "body", fontName=SERIF, fontSize=10.2, leading=14.5,
        textColor=HexColor(INK), alignment=TA_JUSTIFY, spaceAfter=7)
    h1 = ParagraphStyle(
        "h1", fontName=SERIF_B, fontSize=17, leading=20,
        textColor=HexColor(ACCENT_DARK), spaceBefore=12, spaceAfter=3)
    h2 = ParagraphStyle(
        "h2", fontName=SERIF_B, fontSize=13.5, leading=17,
        textColor=HexColor(ACCENT_DARK), spaceBefore=11, spaceAfter=3)
    h3 = ParagraphStyle(
        "h3", fontName=SERIF_BI, fontSize=11.5, leading=15,
        textColor=HexColor(INK), spaceBefore=8, spaceAfter=2)
    bullet = ParagraphStyle(
        "bullet", parent=body, alignment=TA_LEFT, spaceAfter=3)
    code = ParagraphStyle(
        "code", fontName=MONO, fontSize=8.3, leading=10.8,
        textColor=HexColor(INK), backColor=HexColor(CODE_BG),
        borderColor=HexColor(HAIRLINE), borderWidth=0.5, borderPadding=6,
        leftIndent=2, spaceBefore=2, spaceAfter=8)
    cell = ParagraphStyle(
        "cell", fontName=SERIF, fontSize=8.6, leading=11,
        textColor=HexColor(INK), alignment=TA_LEFT)
    cell_h = ParagraphStyle(
        "cell_h", fontName=SANS_B, fontSize=8.6, leading=11,
        textColor=HexColor(ACCENT_DARK), alignment=TA_LEFT)
    caption = ParagraphStyle(
        "caption", fontName=SANS, fontSize=8.2, leading=10.5,
        textColor=HexColor(MUTED), alignment=TA_LEFT, spaceBefore=1, spaceAfter=8)
    return {"body": body, "h1": h1, "h2": h2, "h3": h3, "bullet": bullet,
            "code": code, "cell": cell, "cell_h": cell_h, "caption": caption}


# ---------------------------------------------------------------------------
# Markdown -> styled reportlab flowables
# ---------------------------------------------------------------------------

def _accent_rule(width_ratio: float = 0.28, color: str = ACCENT, thick: float = 1.4):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus.flowables import HRFlowable
    return HRFlowable(width=f"{int(width_ratio * 100)}%", thickness=thick,
                      color=HexColor(color), spaceBefore=1, spaceAfter=7,
                      lineCap="round", hAlign="LEFT")


def _looks_like_table_row(s: str) -> bool:
    return s.startswith("|") and s.count("|") >= 2


def _is_table_separator(s: str) -> bool:
    return bool(re.match(r"^\|?[\s:|-]+\|[\s:|-]*$", s)) and "-" in s


def _split_row(s: str) -> list[str]:
    s = s.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _make_table(rows: list[list[str]], styles: dict, avail_width: float):
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Paragraph, Table, TableStyle

    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    header, body_rows = rows[0], rows[1:]

    data = [[Paragraph(_md_inline(c), styles["cell_h"]) for c in header]]
    for r in body_rows:
        data.append([Paragraph(_md_inline(c), styles["cell"]) for c in r])

    col_w = [avail_width / ncols] * ncols
    tbl = Table(data, colWidths=col_w, hAlign="LEFT", repeatRows=1)
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), HexColor(ACCENT_TINT)),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, HexColor(ACCENT)),
        ("GRID", (0, 0), (-1, -1), 0.4, HexColor(HAIRLINE)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            ts.append(("BACKGROUND", (0, i), (-1, i), HexColor("#FAFBFF")))
    tbl.setStyle(TableStyle(ts))
    return tbl


def render_markdown_flowables(md_text: str, styles: dict, avail_width: float) -> list:
    """Render markdown (headings/bold/italic/code/lists/simple tables/rules) into
    a list of styled reportlab flowables. Robust to malformed input."""
    from reportlab.platypus import (ListFlowable, ListItem, Paragraph,
                                    Preformatted, Spacer)

    flow: list[Any] = []
    lines = md_text.splitlines()
    i = 0
    bullets: list[str] = []

    def flush_bullets() -> None:
        nonlocal bullets
        if bullets:
            flow.append(ListFlowable(
                [ListItem(Paragraph(b, styles["bullet"]), leftIndent=14,
                          value="circle") for b in bullets],
                bulletType="bullet", start="circle", leftIndent=10))
            flow.append(Spacer(1, 3))
            bullets = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_bullets()
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # closing fence
            try:
                flow.append(Preformatted("\n".join(buf) or " ", styles["code"]))
            except Exception:  # noqa: BLE001
                flow.append(Paragraph(_md_inline("\n".join(buf)), styles["body"]))
            continue

        # markdown table (header row + separator + body)
        if _looks_like_table_row(stripped) and i + 1 < len(lines) \
                and _is_table_separator(lines[i + 1].strip()):
            flush_bullets()
            header = _split_row(stripped)
            i += 2  # skip header + separator
            body_rows = []
            while i < len(lines) and _looks_like_table_row(lines[i].strip()):
                body_rows.append(_split_row(lines[i].strip()))
                i += 1
            tbl = _make_table([header, *body_rows], styles, avail_width)
            if tbl is not None:
                flow.append(tbl)
                flow.append(Spacer(1, 8))
            continue

        # blank line
        if not stripped:
            flush_bullets()
            flow.append(Spacer(1, 4))
            i += 1
            continue

        # horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_bullets()
            flow.append(_accent_rule(1.0, HAIRLINE, 0.6))
            i += 1
            continue

        # headings
        if stripped.startswith("### "):
            flush_bullets()
            flow.append(Paragraph(_md_inline(stripped[4:]), styles["h3"]))
        elif stripped.startswith("## "):
            flush_bullets()
            flow.append(Paragraph(_md_inline(stripped[3:]), styles["h2"]))
            flow.append(_accent_rule(0.22))
        elif stripped.startswith("# "):
            flush_bullets()
            flow.append(Paragraph(_md_inline(stripped[2:]), styles["h1"]))
            flow.append(_accent_rule(0.30))
        elif re.match(r"^[-*+]\s+", stripped):
            bullets.append(_md_inline(re.sub(r"^[-*+]\s+", "", stripped)))
        elif re.match(r"^\d+\.\s+", stripped):
            flush_bullets()
            flow.append(Paragraph(_md_inline(stripped), styles["body"]))
        else:
            flush_bullets()
            flow.append(Paragraph(_md_inline(stripped), styles["body"]))
        i += 1

    flush_bullets()
    return flow


# ---------------------------------------------------------------------------
# Cover page + footer (drawn directly on the canvas)
# ---------------------------------------------------------------------------

def _draw_footer(c, doc) -> None:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import LETTER
    w, _h = LETTER
    y = 0.55 * 72
    c.saveState()
    c.setStrokeColor(HexColor(HAIRLINE))
    c.setLineWidth(0.5)
    c.line(doc.leftMargin, y + 10, w - doc.rightMargin, y + 10)
    c.setFont(SANS, 7.5)
    c.setFillColor(HexColor(MUTED))
    c.drawString(doc.leftMargin, y, FOOTER_LEFT)
    c.drawRightString(w - doc.rightMargin, y, f"page {doc.page}")
    c.restoreState()


def _make_cover_drawer(meta: dict, key_fig: str | None):
    def _draw_cover(c, doc) -> None:
        from reportlab.lib.colors import HexColor
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.utils import ImageReader
        from reportlab.platypus import Paragraph
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_CENTER

        w, h = LETTER
        margin = doc.leftMargin
        cw = w - 2 * margin
        acc = HexColor(ACCENT)
        c.saveState()

        # top accent band
        c.setFillColor(acc)
        c.rect(0, h - 8, w, 8, fill=1, stroke=0)
        c.setFillColor(HexColor(SECONDARY))
        c.rect(0, h - 8, w * 0.34, 8, fill=1, stroke=0)

        # eyebrow label
        top = h - 1.55 * 72
        c.setFont(SANS_B, 10)
        c.setFillColor(HexColor(MUTED))
        c.drawCentredString(w / 2, top, "R E P R O D U C T I O N   S U M M A R Y")

        # title (large serif, centered, wrapped)
        title = str(meta.get("title") or "Reproduction Summary")
        tstyle = ParagraphStyle("cover_title", fontName=SERIF_B, fontSize=26,
                                leading=30, alignment=TA_CENTER,
                                textColor=HexColor(INK))
        para = Paragraph(escape(title), tstyle)
        _tw, th = para.wrap(cw, h)
        ty = top - 26 - th
        para.drawOn(c, margin, ty)

        # short accent rule under the title
        c.setStrokeColor(acc)
        c.setLineWidth(1.6)
        c.line(w / 2 - 40, ty - 12, w / 2 + 40, ty - 12)

        # area chip (rounded, filled with the area color, white text)
        cursor = ty - 44
        code = canon_area(meta.get("area"))
        if code:
            label = area_label(code)
            chip_col = HexColor(area_color(code))
            c.setFont(SANS_B, 10.5)
            pad = 12
            tw = c.stringWidth(label, SANS_B, 10.5)
            chip_w = tw + 2 * pad
            chip_h = 22
            cx = w / 2 - chip_w / 2
            c.setFillColor(chip_col)
            c.roundRect(cx, cursor - chip_h, chip_w, chip_h, 6, fill=1, stroke=0)
            c.setFillColor(HexColor("#FFFFFF"))
            c.drawCentredString(w / 2, cursor - chip_h + 6.5, label)
            cursor -= chip_h + 16

        # date
        date = str(meta.get("date") or datetime.now().strftime("%Y-%m-%d"))
        c.setFont(SERIF_I, 11.5)
        c.setFillColor(HexColor(INK))
        c.drawCentredString(w / 2, cursor, date)
        cursor -= 20

        # wordmark
        c.setFont(SANS, 9.5)
        c.setFillColor(HexColor(MUTED))
        c.drawCentredString(w / 2, cursor, WORDMARK)
        cursor -= 6

        # optional repo url
        repo = meta.get("repo_url")
        if repo:
            c.setFont(MONO, 8)
            c.setFillColor(HexColor(ACCENT_DARK))
            c.drawCentredString(w / 2, cursor - 10, str(repo)[:90])
            cursor -= 16

        # thin accent rule, then the featured figure below it
        if key_fig and Path(key_fig).exists():
            try:
                rule_y = cursor - 18
                c.setStrokeColor(acc)
                c.setLineWidth(0.8)
                c.line(margin + cw * 0.20, rule_y, margin + cw * 0.80, rule_y)

                img = ImageReader(key_fig)
                iw, ih = img.getSize()
                max_w = cw * 0.78
                bottom_limit = 1.35 * 72  # keep clear of footer
                max_h = (rule_y - 26) - bottom_limit
                if iw > 0 and ih > 0 and max_h > 60:
                    scale = min(max_w / iw, max_h / ih)
                    dw, dh = iw * scale, ih * scale
                    ix = w / 2 - dw / 2
                    iy = rule_y - 22 - dh
                    c.drawImage(img, ix, iy, width=dw, height=dh,
                                preserveAspectRatio=True, mask="auto")
                    c.setFont(SANS, 8)
                    c.setFillColor(HexColor(MUTED))
                    cap = "Key reproduced figure: " + Path(key_fig).name
                    c.drawCentredString(w / 2, iy - 12, cap[:96])
            except Exception:  # noqa: BLE001
                pass

        # slim cover footer
        c.setStrokeColor(HexColor(HAIRLINE))
        c.setLineWidth(0.5)
        c.line(margin, 0.62 * 72, w - margin, 0.62 * 72)
        c.setFont(SANS, 7.5)
        c.setFillColor(HexColor(MUTED))
        c.drawString(margin, 0.5 * 72, FOOTER_LEFT)
        c.drawRightString(w - margin, 0.5 * 72, "cover")
        c.restoreState()

    return _draw_cover


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def build_summary_pdf(summary_md_path, out_pdf_path, meta: dict | None = None) -> bool:
    """Render a reproduction summary markdown file into a branded PDF.

    ``meta`` (all optional): title, area, date, key_figure_path, repo_url.
    Missing figure/meta never crash the build. Returns True on success.
    """
    meta = dict(meta or {})
    summary_md_path = Path(summary_md_path)
    out_pdf_path = Path(out_pdf_path)

    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.units import inch
        from reportlab.platypus import (BaseDocTemplate, Frame, NextPageTemplate,
                                        PageBreak, PageTemplate, Paragraph)
    except Exception:  # noqa: BLE001
        return False

    # --- source text -----------------------------------------------------
    md_text = ""
    try:
        md_text = summary_md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        md_text = ""

    # --- resolve meta (robust) ------------------------------------------
    if not meta.get("title"):
        meta["title"] = (first_h1_title(summary_md_path)
                         or humanize_title(summary_md_path.parent.name))
    if not meta.get("date"):
        meta["date"] = datetime.now().strftime("%Y-%m-%d")
    key_fig = meta.get("key_figure_path")
    if key_fig is None:
        key_fig = pick_key_figure(summary_md_path.parent)
    if key_fig and not Path(str(key_fig)).exists():
        key_fig = None

    styles = _styles()
    margin = 0.92 * inch
    avail_width = LETTER[0] - 2 * margin

    # drop the leading H1 from the body (it is already the cover title)
    body_src = re.sub(r"\A\s*#\s+.*(?:\r?\n)+", "", md_text, count=1)
    try:
        body_flow = render_markdown_flowables(body_src, styles, avail_width)
    except Exception:  # noqa: BLE001
        body_flow = [Paragraph(escape(md_text[:4000]) or "(empty summary)",
                               styles["body"])]
    if not body_flow:
        body_flow = [Paragraph("(no summary content)", styles["body"])]

    story: list[Any] = [NextPageTemplate("body"), PageBreak(), *body_flow]

    try:
        out_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        doc = BaseDocTemplate(
            str(out_pdf_path), pagesize=LETTER,
            leftMargin=margin, rightMargin=margin,
            topMargin=margin, bottomMargin=0.9 * inch,
            title=str(meta.get("title"))[:120],
            author="AI-DS-ML-DL Researcher")
        cover_frame = Frame(0, 0, LETTER[0], LETTER[1], id="cover",
                            leftPadding=0, rightPadding=0,
                            topPadding=0, bottomPadding=0)
        body_frame = Frame(margin, 0.9 * inch, avail_width,
                           LETTER[1] - margin - 0.9 * inch, id="body")
        doc.addPageTemplates([
            PageTemplate(id="cover", frames=[cover_frame],
                         onPage=_make_cover_drawer(meta, str(key_fig) if key_fig else None)),
            PageTemplate(id="body", frames=[body_frame], onPage=_draw_footer),
        ])
        doc.build(story)
        return out_pdf_path.exists()
    except Exception:  # noqa: BLE001
        return False
