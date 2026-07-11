#!/usr/bin/env python3
"""
Caption-aware figure extraction for the reproduction harness.

The original extractor only pulled *embedded raster* images out of a PDF, which
silently missed every VECTOR figure (matplotlib / astropy / TikZ plots drawn as
paths rather than pixels). Those are exactly the figures most papers care about.

This module rebuilds extraction around the PDF **text layer**:

  1. Scan every page's text blocks for caption lines -- "Figure N", "Fig. N",
     "FIG. N", "FIGURE N" -- and record each caption's bounding box + number +
     text (PyMuPDF ``page.get_text("blocks")``).
  2. For each caption, compute the figure REGION sitting directly above it in the
     same column by unioning the page's vector-drawing rects and embedded-image
     rects that fall in that band, then render that region to a PNG at ~200 DPI
     (``page.get_pixmap(clip=...)``). This captures vector figures faithfully.
  3. Still pull large embedded rasters (photos/screenshots) so nothing is lost.
  4. De-duplicate near-identical crops with a tiny average-hash so a raster that
     is also captured by its region render is not written twice.
  5. Name files ``fig-<NN>-<caption-slug>.png`` and write ``captions.json``
     mapping each file -> its caption text.
  6. If ``pytesseract`` + a real ``tesseract`` binary are present, OCR image-only
     figures for extra labels; otherwise rely purely on the text layer. Tesseract
     is NEVER hard-required.

Everything is wrapped defensively: on any error the extractor degrades to the old
raster/page-render behaviour and always returns a count. It never raises, so the
unattended pipeline cannot crash here.

Public API:
    extract_figures(pdf_path, out_dir, dpi=200, max_figures=40,
                    min_raster_pixels=8100) -> dict
        -> {"count": int, "captions": {file: caption}, "vector": int,
            "raster": int, "ocr": bool, "note": str}

CLI:
    python figure_extract.py <paper.pdf> <out_dir> [--dpi 200] [--max 40]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# caption detection
# -----------------------------------------------------------------------------
# Matches a block whose FIRST line begins a figure caption. Requires a caption
# separator (":" / "." / ")" / dash) right after the number -- this is what
# distinguishes a real caption ("Figure 3: ...", "Figure 3. ...") from an in-text
# reference that merely starts a sentence ("Figure 3 and Table 2 show ..."). Also
# anchored at the start so it never matches "Table N" or mid-sentence mentions.
_CAPTION_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?|FIG\.?|FIGURE)\s+"
    r"([0-9]{1,3}|[IVXLC]{1,5})\s*[\.:\)\-–—]\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)


def _caption_match(block_text: str) -> tuple[str, str] | None:
    """Return (number, caption_text) if the block starts a figure caption."""
    txt = (block_text or "").strip()
    if not txt:
        return None
    # only inspect the first non-empty line for the "Figure N" trigger
    first = txt.splitlines()[0].strip()
    m = _CAPTION_RE.match(first)
    if not m:
        return None
    num = m.group(1)
    # collapse the whole caption (may span several lines) into one clean string
    caption = " ".join(txt.split())
    return num, caption


def slugify(text: str, max_len: int = 48) -> str:
    """Filesystem-safe lowercase-hyphen slug from arbitrary caption text."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:max_len].strip("-")) or "figure"


def _caption_slug(number: str, caption: str) -> str:
    """Slug built from the words *after* the 'Figure N' prefix (first ~8 words)."""
    body = _CAPTION_RE.match(caption.strip())
    tail = body.group(2) if body else caption
    words = re.findall(r"[A-Za-z0-9]+", tail)[:8]
    slug = slugify(" ".join(words)) if words else ""
    return slug or "caption"


# -----------------------------------------------------------------------------
# average-hash de-duplication (no PIL/numpy dependency; pure PyMuPDF)
# -----------------------------------------------------------------------------

def _ahash(pix: Any) -> int | None:
    """64-bit average hash of a PyMuPDF pixmap (grayscale, ~8x8). None on error."""
    try:
        import fitz
        g = pix
        if g.n - g.alpha >= 3:            # convert colour -> gray
            g = fitz.Pixmap(fitz.csGRAY, g)
        elif g.alpha:                     # drop alpha
            g = fitz.Pixmap(g, 0)
        g = fitz.Pixmap(g)                # copy we can shrink in place
        guard = 0
        while (g.width > 8 or g.height > 8) and guard < 12:
            g.shrink(1)                   # halves each dimension
            guard += 1
        data = g.samples
        if not data:
            return None
        vals = list(data)
        avg = sum(vals) / len(vals)
        bits = 0
        for i, v in enumerate(vals[:64]):
            if v >= avg:
                bits |= (1 << i)
        return bits
    except Exception:  # noqa: BLE001
        return None


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _is_dup(h: int | None, seen: list[int], thresh: int = 8) -> bool:
    if h is None:
        return False
    for prev in seen:
        if _hamming(h, prev) <= thresh:
            return True
    return False


# -----------------------------------------------------------------------------
# region geometry
# -----------------------------------------------------------------------------

def _clamp_rect(r: Any, page_rect: Any) -> Any:
    """Intersect a rect with the page; return None if empty/degenerate."""
    import fitz
    x0 = max(r.x0, page_rect.x0)
    y0 = max(r.y0, page_rect.y0)
    x1 = min(r.x1, page_rect.x1)
    y1 = min(r.y1, page_rect.y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return fitz.Rect(x0, y0, x1, y1)


def _column_bounds(caption_rect: Any, page_rect: Any) -> tuple[float, float]:
    """Horizontal (x0, x1) of the column the caption lives in.

    Full width if the caption spans most of the page; otherwise the left or
    right half (two-column layout) chosen by the caption's centre.
    """
    W = page_rect.width
    cap_w = caption_rect.width
    mid = page_rect.x0 + W / 2.0
    margin = W * 0.04
    if cap_w >= 0.62 * W:                 # spans the page -> single column
        return (page_rect.x0 + margin, page_rect.x1 - margin)
    cx = (caption_rect.x0 + caption_rect.x1) / 2.0
    if cx < mid:                          # left column
        return (page_rect.x0 + margin, mid + margin)
    return (mid - margin, page_rect.x1 - margin)


def _graphic_rects(page: Any, page_rect: Any) -> list[Any]:
    """Bounding rects of vector drawings + embedded images on the page."""
    import fitz
    rects: list[Any] = []
    try:
        for d in page.get_drawings():
            r = d.get("rect")
            if r is None:
                continue
            cr = _clamp_rect(fitz.Rect(r), page_rect)
            if cr is not None:
                rects.append(cr)
    except Exception:  # noqa: BLE001
        pass
    try:
        for info in page.get_image_info():
            bb = info.get("bbox")
            if not bb:
                continue
            cr = _clamp_rect(fitz.Rect(bb), page_rect)
            if cr is not None:
                rects.append(cr)
    except Exception:  # noqa: BLE001
        pass
    return rects


def _figure_region(page: Any, caption_rect: Any, prev_bottom: float,
                   graphics: list[Any] | None = None) -> Any:
    """Best-effort bbox of the figure sitting ABOVE a caption in its column.

    prev_bottom = y1 of the nearest caption above this one in the same column
    (so two stacked figures are not merged). ``graphics`` is the page's cached
    vector/image rects (computed once per page to avoid re-scanning drawings).
    Falls back to a fixed band above the caption when no graphics are found.
    """
    import fitz
    page_rect = page.rect
    if graphics is None:
        graphics = _graphic_rects(page, page_rect)
    col_x0, col_x1 = _column_bounds(caption_rect, page_rect)
    top_limit = max(page_rect.y0, prev_bottom)
    # candidate graphics: in-column, above the caption, below the previous caption
    cands = []
    for r in graphics:
        cx = (r.x0 + r.x1) / 2.0
        if cx < col_x0 - 2 or cx > col_x1 + 2:
            continue
        if r.y1 > caption_rect.y0 + 3:     # not above the caption
            continue
        if r.y1 < top_limit - 2:           # above the previous caption
            continue
        cands.append(r)
    if cands:
        gx0 = min(r.x0 for r in cands)
        gy0 = min(r.y0 for r in cands)
        gx1 = max(r.x1 for r in cands)
        # include the caption line itself so the crop is self-describing
        rect = fitz.Rect(min(gx0, caption_rect.x0), gy0,
                         max(gx1, caption_rect.x1), caption_rect.y1)
    else:
        # fallback: a band above the caption, height <= 55% of the page
        band_h = min(caption_rect.y0 - top_limit, page_rect.height * 0.55)
        if band_h < 40:
            band_h = min(page_rect.height * 0.35, caption_rect.y0 - page_rect.y0)
        rect = fitz.Rect(col_x0, caption_rect.y0 - band_h,
                        col_x1, caption_rect.y1)
    # small padding, then clamp
    pad = 4
    rect = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
    return _clamp_rect(rect, page_rect)


# -----------------------------------------------------------------------------
# optional OCR (never hard-required)
# -----------------------------------------------------------------------------

def _ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        import shutil
        if shutil.which("tesseract"):
            return True
        # honour an explicit cmd path if the user configured one
        try:
            cmd = pytesseract.pytesseract.tesseract_cmd
            return bool(cmd) and Path(cmd).exists()
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False


def _ocr_pixmap(pix: Any) -> str:
    """Best-effort OCR of a pixmap -> short label string ('' on any failure)."""
    try:
        import io
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        text = pytesseract.image_to_string(img)
        text = " ".join(text.split())
        return text[:200]
    except Exception:  # noqa: BLE001
        return ""


# -----------------------------------------------------------------------------
# main entry point
# -----------------------------------------------------------------------------

def extract_figures(pdf_path: str | Path, out_dir: str | Path, dpi: int = 200,
                    max_figures: int = 40, min_raster_pixels: int = 90 * 90,
                    time_budget_s: float = 240.0) -> dict[str, Any]:
    """Extract captioned vector + raster figures from a PDF into ``out_dir``.

    ``time_budget_s`` is a soft wall-clock cap: drawing-heavy PDFs (dense contour
    plots) can be slow to scan, so the pass loops stop early once the budget is
    spent, keeping the unattended pipeline responsive. Returns a summary dict
    (see module docstring). Never raises.
    """
    import time as _time
    _deadline = _time.monotonic() + max(30.0, float(time_budget_s))
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    result: dict[str, Any] = {"count": 0, "captions": {}, "vector": 0,
                              "raster": 0, "ocr": False, "note": ""}
    if not pdf_path.exists():
        result["note"] = "pdf-missing"
        return result
    try:
        import fitz  # PyMuPDF
    except Exception:  # noqa: BLE001
        result["note"] = "pymupdf-unavailable"
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"open-failed:{exc.__class__.__name__}"
        return result

    ocr_on = _ocr_available()
    result["ocr"] = ocr_on
    zoom = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    seen_hashes: list[int] = []
    used_names: set[str] = set()
    captions_map: dict[str, str] = {}
    written = 0

    def _unique_name(number: str, slug: str) -> str:
        base = f"fig-{_pad(number)}-{slug}"
        name = f"{base}.png"
        i = 2
        while name in used_names or (out_dir / name).exists():
            name = f"{base}-{i}.png"
            i += 1
        used_names.add(name)
        return name

    try:
        # -- pass 1: caption-anchored region renders (captures VECTOR figures) --
        for pno in range(doc.page_count):
            if written >= max_figures or _time.monotonic() > _deadline:
                break
            page = doc[pno]
            try:
                blocks = page.get_text("blocks")
            except Exception:  # noqa: BLE001
                blocks = []
            # collect caption blocks on this page, ordered top->bottom
            caps = []
            for b in blocks:
                if len(b) < 5:
                    continue
                # text blocks only (block_type 0); tolerate short tuples
                btype = b[6] if len(b) > 6 else 0
                if btype != 0:
                    continue
                m = _caption_match(b[4])
                if not m:
                    continue
                caps.append({"rect": fitz.Rect(b[0], b[1], b[2], b[3]),
                             "num": m[0], "text": m[1]})
            if not caps:
                continue
            caps.sort(key=lambda c: c["rect"].y0)
            # cache the page's vector/image rects ONCE (get_drawings is expensive)
            try:
                page_graphics = _graphic_rects(page, page.rect)
            except Exception:  # noqa: BLE001
                page_graphics = []
            # previous-caption bottom per column, to avoid merging stacked figs
            prev_bottom_by_col: dict[str, float] = {}
            for c in caps:
                if written >= max_figures:
                    break
                col = _col_key(c["rect"], page.rect)
                prev_bottom = prev_bottom_by_col.get(col, page.rect.y0)
                region = _figure_region(page, c["rect"], prev_bottom,
                                        graphics=page_graphics)
                prev_bottom_by_col[col] = c["rect"].y1
                if region is None:
                    continue
                try:
                    pix = page.get_pixmap(matrix=zoom, clip=region, alpha=False)
                except Exception:  # noqa: BLE001
                    continue
                if pix.width < 40 or pix.height < 40:
                    continue
                h = _ahash(pix)
                if _is_dup(h, seen_hashes):
                    continue
                name = _unique_name(c["num"], _caption_slug(c["num"], c["text"]))
                try:
                    pix.save(str(out_dir / name))
                except Exception:  # noqa: BLE001
                    continue
                if h is not None:
                    seen_hashes.append(h)
                captions_map[name] = c["text"]
                written += 1
                result["vector"] += 1

        # -- pass 2: large embedded rasters not already captured ----------------
        for pno in range(doc.page_count):
            if written >= max_figures or _time.monotonic() > _deadline:
                break
            page = doc[pno]
            try:
                imgs = page.get_images(full=True)
            except Exception:  # noqa: BLE001
                imgs = []
            for img in imgs:
                if written >= max_figures:
                    break
                xref = img[0]
                try:
                    ext = doc.extract_image(xref)
                except Exception:  # noqa: BLE001
                    continue
                w, h = ext.get("width", 0), ext.get("height", 0)
                if w * h < min_raster_pixels:
                    continue
                try:
                    pm = fitz.Pixmap(doc, xref)
                except Exception:  # noqa: BLE001
                    pm = None
                ah = _ahash(pm) if pm is not None else None
                if _is_dup(ah, seen_hashes):
                    continue
                # name it from a caption on the same page if we can find one
                num, cap_text = _nearest_caption_for_image(page, xref)
                slug = _caption_slug(num, cap_text) if cap_text else "embedded"
                name = _unique_name(num or "00", slug)
                try:
                    (out_dir / name).write_bytes(ext["image"])
                except Exception:  # noqa: BLE001
                    continue
                if ah is not None:
                    seen_hashes.append(ah)
                label = cap_text
                if not label and ocr_on and pm is not None:
                    ocr_txt = _ocr_pixmap(pm)
                    if ocr_txt:
                        label = f"(OCR labels) {ocr_txt}"
                captions_map[name] = label or "Embedded figure (no caption detected)"
                written += 1
                result["raster"] += 1

        # -- fallback: nothing found -> render the first few pages --------------
        if written == 0:
            for pno in range(min(doc.page_count, 6)):
                try:
                    pix = doc[pno].get_pixmap(dpi=140)
                    name = f"page-{pno + 1:02d}.png"
                    pix.save(str(out_dir / name))
                    captions_map[name] = f"Full page {pno + 1} (no captions detected)"
                    written += 1
                except Exception:  # noqa: BLE001
                    continue
            result["note"] = "fallback-page-render"
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass

    result["count"] = written
    result["captions"] = captions_map

    # captions.json (file -> caption) for downstream steps + the driver prompt
    try:
        (out_dir / "captions.json").write_text(
            json.dumps(captions_map, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    # human-readable note the reproduction prompt already points at
    try:
        note = [
            "# Auto-extracted figures",
            "",
            f"{written} image(s) were auto-extracted from paper.pdf by the harness "
            f"({result['vector']} vector-region render(s), {result['raster']} "
            f"embedded raster(s)).",
            "",
            "Vector figures (matplotlib/plot-style) are captured by rendering the "
            "page region above each detected caption; embedded rasters are pulled "
            "directly. `captions.json` maps each file to its caption text.",
            "",
            ("OCR was applied to image-only figures." if result["ocr"]
             else "Tesseract OCR was not available; extraction used the PDF text "
                  "layer only (this is expected and fine)."),
            "",
            "These are the paper's ORIGINAL figures. A few crops may be imperfect "
            "or decorative - prune those. If a KEY figure is missing, extract it "
            "from paper.pdf.",
        ]
        (out_dir / "EXTRACTION_NOTE.md").write_text("\n".join(note), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    return result


# -----------------------------------------------------------------------------
# small helpers used above
# -----------------------------------------------------------------------------

def _pad(number: str) -> str:
    """Zero-pad numeric figure numbers to 2 digits; pass roman/other through."""
    try:
        return f"{int(number):02d}"
    except (ValueError, TypeError):
        return slugify(str(number), 6) or "00"


def _col_key(caption_rect: Any, page_rect: Any) -> str:
    x0, x1 = _column_bounds(caption_rect, page_rect)
    mid = page_rect.x0 + page_rect.width / 2.0
    if x1 - x0 >= 0.6 * page_rect.width:
        return "full"
    return "left" if (caption_rect.x0 + caption_rect.x1) / 2.0 < mid else "right"


def _nearest_caption_for_image(page: Any, xref: int) -> tuple[str, str]:
    """Find the caption whose top sits just below the image's bbox, if any."""
    import fitz
    try:
        rects = page.get_image_rects(xref)
    except Exception:  # noqa: BLE001
        rects = []
    if not rects:
        return ("", "")
    img_rect = rects[0]
    best: tuple[float, str, str] | None = None
    try:
        blocks = page.get_text("blocks")
    except Exception:  # noqa: BLE001
        blocks = []
    for b in blocks:
        if len(b) < 5:
            continue
        m = _caption_match(b[4])
        if not m:
            continue
        cap_rect = fitz.Rect(b[0], b[1], b[2], b[3])
        gap = cap_rect.y0 - img_rect.y1
        if gap < -20 or gap > 140:         # caption should be below the image
            continue
        if best is None or gap < best[0]:
            best = (gap, m[0], m[1])
    if best is None:
        return ("", "")
    return (best[1], best[2])


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract captioned vector + raster figures from a PDF.")
    ap.add_argument("pdf", type=Path, help="path to the paper PDF")
    ap.add_argument("out_dir", type=Path, help="output directory (original_results/)")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--max", type=int, default=40, dest="max_figures")
    args = ap.parse_args(argv)
    info = extract_figures(args.pdf, args.out_dir, dpi=args.dpi,
                           max_figures=args.max_figures)
    print(f"[figure_extract] wrote {info['count']} figure(s) "
          f"(vector={info['vector']} raster={info['raster']} "
          f"ocr={info['ocr']}) -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
