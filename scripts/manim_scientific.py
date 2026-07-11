#!/usr/bin/env python3
"""
manim_scientific -- branded, data-driven graphical scene primitives for Manim
Community Edition 0.19, in the 3Blue1Brown style.

This module is a *reusable library*: it is imported by the auto-generated
``scene.py`` that ``make_manim.py`` writes into every paper's ``manim/`` folder,
and it can also be imported directly to compose custom scientific scenes.

Design rules (project constraints):
  * Manim Community 0.19 only.
  * **Pango Text / MarkupText ONLY -- never LaTeX** (``Tex``/``MathTex``), since
    a TeX install may be absent on the target machine. Axis numbers, tick
    labels and value callouts are all rendered with ``Text``.
  * Every primitive is defensive: bad / empty / non-finite data is sanitised,
    and helpers degrade to *something graphical* rather than raising.
  * A consistent dark "brand" theme (see the colour constants below), with a
    per-research-area accent colour.

The primitives fall into a few families:
  - THEME + text helpers            : brand colours, font pick, ``T`` / ``MT``.
  - CARDS                           : ``backdrop``, ``title_card``, ``outro_card``.
  - AXES                            : ``branded_axes``, ``axis_titles``,
                                      ``add_text_ticks``.
  - DATA PLOTS (animated)           : ``animated_line_plot``, ``reveal_curve``,
                                      ``animated_scatter``, ``show_bars``,
                                      ``grouped_bar_compare``.
  - GEOMETRY / TRANSFORMS           : ``transform_shapes``, ``vector_sweep``,
                                      ``number_line_demo``, ``conceptual_diagram``.
  - EMPHASIS                        : ``highlight``.

Most "show_*" style helpers take the live ``Scene`` as their first argument,
build their mobjects, animate them in, and *return* a ``VGroup`` of everything
created so the caller can fade it out cleanly.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Sequence

from manim import (
    Axes, NumberPlane, NumberLine,
    Scene, Text, MarkupText, VGroup, VMobject,
    Rectangle, RoundedRectangle, Line, DashedLine, Dot, Arrow, Vector,
    Polygon, RegularPolygon, Circle, Square, Triangle,
    UP, DOWN, LEFT, RIGHT, UL, UR, DL, DR, ORIGIN,
    PI, TAU,
    FadeIn, FadeOut, Write, GrowFromEdge, GrowArrow, Create, DrawBorderThenFill,
    Transform, ReplacementTransform, MoveAlongPath,
    Indicate, Flash, Circumscribe, ShowPassingFlash,
    ValueTracker, always_redraw,
    smooth, linear, there_and_back, rate_functions,
)

# ---------------------------------------------------------------------------
# BRAND THEME
# ---------------------------------------------------------------------------

BG = "#0A0E1A"        # deep navy background
PRIMARY = "#E8ECF6"   # primary text / lines
MUTED = "#8A93AD"     # secondary text, ticks, faint rules
ACCENT = "#6D7BFF"    # brand accent (used for the "Paper" reference series)
ACCENT2 = "#34E0E0"   # secondary accent (teal)
GRID = "#1B2236"      # very faint grid colour

# per research-area accent + human-readable names
AREA_COLORS = {"AI": "#A78BFA", "DS": "#22D3EE", "ML": "#34D399", "DL": "#FBBF24"}
AREA_NAMES = {"AI": "Artificial Intelligence", "DS": "Data Science",
              "ML": "Machine Learning", "DL": "Deep Learning"}

FRAME_W = 12.0   # usable horizontal span (inside Manim's ~14.2 default frame)
FRAME_H = 7.2


def area_color(area: str) -> str:
    return AREA_COLORS.get((area or "").upper(), ACCENT)


def area_name(area: str) -> str:
    return AREA_NAMES.get((area or "").upper(), "Research")


# ---------------------------------------------------------------------------
# FONT + TEXT HELPERS  (Pango only -- no LaTeX anywhere)
# ---------------------------------------------------------------------------

def pick_font() -> str | None:
    """Prefer a clean sans; fall back to Manim's default (never crashes)."""
    candidates = ["Inter", "Segoe UI", "Helvetica Neue", "Helvetica",
                  "Arial", "DejaVu Sans", "Liberation Sans"]
    try:
        import manimpango
        available = set(manimpango.list_fonts())
        for c in candidates:
            if c in available:
                return c
    except Exception:
        pass
    return None


FONT = pick_font()


def T(s: Any, **kw) -> Text:
    """``Text`` with the brand font + colour applied by default."""
    if FONT and "font" not in kw:
        kw["font"] = FONT
    kw.setdefault("color", PRIMARY)
    return Text("" if s is None else str(s), **kw)


def MT(s: Any, **kw) -> MarkupText:
    """``MarkupText`` (Pango markup) with the brand font applied."""
    if FONT and "font" not in kw:
        kw["font"] = FONT
    kw.setdefault("color", PRIMARY)
    return MarkupText("" if s is None else str(s), **kw)


def fit(mobject, max_width: float = FRAME_W, max_height: float | None = None):
    """Scale a mobject down so it stays within the frame (never scales up)."""
    try:
        if mobject.width > max_width > 0:
            mobject.scale(max_width / mobject.width)
        if max_height and mobject.height > max_height > 0:
            mobject.scale(max_height / mobject.height)
    except Exception:
        pass
    return mobject


def wrap(text: str, width: int = 40, max_lines: int = 5) -> list[str]:
    """Greedy word-wrap into <= ``max_lines`` lines of ~``width`` chars."""
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    if not lines:
        lines = [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = (lines[-1][:width - 1].rstrip() + "…")
    return lines


def fmt_num(v: Any) -> str:
    """Compact human number formatting (no LaTeX, plain digits)."""
    try:
        v = float(v)
    except Exception:
        return str(v)
    if not math.isfinite(v):
        return "-"
    if v != 0 and (abs(v) >= 10000 or abs(v) < 1e-3):
        return f"{v:.1e}"
    if float(v).is_integer():
        return str(int(v))
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s


# ---------------------------------------------------------------------------
# DATA SANITISERS
# ---------------------------------------------------------------------------

def _finite(seq: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for v in seq or []:
        try:
            f = float(v)
        except Exception:
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _nice_range(lo: float, hi: float, ticks: int = 5) -> tuple[float, float, float]:
    """Return (start, end, step) spanning [lo, hi] with ~``ticks`` round steps."""
    if not (math.isfinite(lo) and math.isfinite(hi)):
        lo, hi = 0.0, 1.0
    if hi <= lo:
        hi = lo + 1.0
    span = hi - lo
    raw = span / max(1, ticks)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    if end <= start:
        end = start + step
    return start, end, step


# ---------------------------------------------------------------------------
# CAMERA  (graceful no-ops on a plain Scene; real moves on MovingCameraScene)
# ---------------------------------------------------------------------------

def camera_frame(scene: Scene):
    """Return the movable camera frame if the scene supports one, else None.

    A plain ``Scene`` has no ``camera.frame``; a ``MovingCameraScene`` does.
    Every camera helper below degrades to a silent no-op when this is None, so
    the same primitives work on either base class.
    """
    try:
        return getattr(scene.camera, "frame", None)
    except Exception:
        return None


def save_camera_home(scene: Scene) -> None:
    """Remember the camera's resting frame so :func:`restore_camera` can return."""
    frame = camera_frame(scene)
    if frame is None:
        return
    try:
        scene._ms_home = (frame.get_center().copy(), float(frame.width))
    except Exception:
        pass


def zoom_to(scene: Scene, mobj, factor: float = 0.62, run_time: float = 1.1,
            shift=None) -> bool:
    """Push the camera in on ``mobj`` (scale the frame by ``factor`` < 1).

    No-op (returns False) when the scene has no movable frame. ``shift`` nudges
    the framing after centring so a callout above the target stays in view.
    """
    frame = camera_frame(scene)
    if frame is None or mobj is None:
        return False
    try:
        target = mobj.get_center() if hasattr(mobj, "get_center") else mobj
        if shift is not None:
            target = target + shift
        scene.play(frame.animate.scale(factor).move_to(target),
                   run_time=run_time, rate_func=smooth)
        return True
    except Exception:
        return False


def restore_camera(scene: Scene, run_time: float = 1.0) -> bool:
    """Return the camera to its saved home (or ORIGIN). No-op without a frame."""
    frame = camera_frame(scene)
    if frame is None:
        return False
    home = getattr(scene, "_ms_home", None)
    try:
        if home:
            center, width = home
            scene.play(frame.animate.move_to(center).set(width=width),
                       run_time=run_time, rate_func=smooth)
        else:
            scene.play(frame.animate.move_to(ORIGIN), run_time=run_time)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CARDS + BACKDROP
# ---------------------------------------------------------------------------

def backdrop(scene: Scene) -> VGroup:
    """A faint dotted grid that stays behind everything. Returns the group."""
    try:
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
        scene.add(dots)
        return dots
    except Exception:
        g = VGroup()
        scene.add(g)
        return g


def area_chip(area: str) -> VGroup:
    """A rounded 'pill' badge naming the research area, filled with its colour."""
    col = area_color(area)
    label = T(area_name(area), color=BG, weight="BOLD", font_size=24)
    pad_x, pad_y = 0.34, 0.16
    pill = RoundedRectangle(
        width=max(label.width + 2 * pad_x, 1.0),
        height=label.height + 2 * pad_y,
        corner_radius=(label.height + 2 * pad_y) / 2,
        stroke_width=0, fill_color=col, fill_opacity=1.0,
    )
    label.move_to(pill.get_center())
    return VGroup(pill, label)


def title_card(scene: Scene, title: str, area: str = "",
               kicker: str = "REPRODUCED FINDING") -> None:
    """Branded opening card: kicker -> bold title -> accent underline -> chip."""
    col = area_color(area)
    kick = T(kicker, color=MUTED, font_size=22)

    title_lines = wrap(title or "Research finding", 30, 4)
    ttl = VGroup(*[T(ln, weight="BOLD", color=PRIMARY, font_size=46)
                   for ln in title_lines])
    ttl.arrange(DOWN, aligned_edge=LEFT, buff=0.16)
    fit(ttl, FRAME_W, 3.2)

    underline = Line(LEFT, RIGHT, color=col, stroke_width=5)
    underline.set_length(min(ttl.width, FRAME_W))

    chip = area_chip(area)

    block = VGroup(kick, ttl, underline, chip)
    block.arrange(DOWN, aligned_edge=LEFT, buff=0.35)
    underline.align_to(ttl, LEFT)
    block.move_to(ORIGIN)

    scene.play(FadeIn(kick, shift=DOWN * 0.2), run_time=0.6, rate_func=smooth)
    scene.play(Write(ttl), run_time=1.3)
    scene.play(Create(underline), run_time=0.5, rate_func=smooth)
    scene.play(FadeIn(chip, shift=UP * 0.15), run_time=0.6, rate_func=smooth)
    scene.wait(1.2)
    scene.play(FadeOut(block, shift=UP * 0.3), run_time=0.6)


def outro_card(scene: Scene, area: str = "", date: str = "") -> None:
    """Branded closing card with wordmark + area + date."""
    col = area_color(area)
    wordmark = T("AI·DS·ML·DL Researcher",
                 weight="BOLD", color=PRIMARY, font_size=44)
    fit(wordmark, FRAME_W)
    rule = Line(LEFT, RIGHT, color=col, stroke_width=4)
    rule.set_length(min(wordmark.width, FRAME_W))
    subline = T(f"{area_name(area)}  ·  reproduced  ·  {date}",
                color=col, font_size=24)
    repo = T("AI_DS_ML_DL_Researcher", color=MUTED, font_size=18)

    block = VGroup(wordmark, rule, subline, repo)
    block.arrange(DOWN, buff=0.3)
    block.move_to(ORIGIN)

    scene.play(Write(wordmark), run_time=1.0)
    scene.play(Create(rule), run_time=0.4, rate_func=smooth)
    scene.play(FadeIn(subline, shift=UP * 0.15), run_time=0.6)
    scene.play(FadeIn(repo), run_time=0.4)
    scene.wait(1.3)
    scene.play(FadeOut(block), run_time=0.6)


def section_label(scene: Scene, text: str, area: str = "") -> Text:
    """A small top-of-frame section heading; returns it so it can be removed."""
    lab = T(text, weight="BOLD", color=PRIMARY, font_size=32)
    fit(lab, FRAME_W)
    lab.to_edge(UP, buff=0.6)
    scene.play(Write(lab), run_time=0.7)
    return lab


# ---------------------------------------------------------------------------
# AXES  (branded; Pango tick labels, no LaTeX)
# ---------------------------------------------------------------------------

def branded_axes(x_range: Sequence[float], y_range: Sequence[float],
                 x_length: float = 8.6, y_length: float = 4.6,
                 area: str = "") -> Axes:
    """
    A dark-theme ``Axes`` sized to sit in the lower ~2/3 of the frame.

    ``x_range`` / ``y_range`` are ``[min, max, step]`` (step optional -> auto).
    Numbers are NOT auto-added (that path can pull in LaTeX); use
    :func:`add_text_ticks` for Pango numeric labels.
    """
    xr = list(x_range)
    yr = list(y_range)
    if len(xr) == 2:
        xr = list(_nice_range(xr[0], xr[1]))
    if len(yr) == 2:
        yr = list(_nice_range(yr[0], yr[1]))
    ax = Axes(
        x_range=xr, y_range=yr,
        x_length=x_length, y_length=y_length,
        axis_config={
            "color": MUTED, "stroke_width": 2.5,
            "include_numbers": False, "include_tip": True,
            "tip_width": 0.16, "tip_height": 0.16,
        },
        tips=True,
    )
    return ax


def add_text_ticks(ax: Axes, x_values: Sequence[float] | None = None,
                   y_values: Sequence[float] | None = None,
                   font_size: int = 18) -> VGroup:
    """Place Pango ``Text`` numeric labels under x ticks / left of y ticks."""
    labels = VGroup()
    try:
        x0, x1, xs = ax.x_range[0], ax.x_range[1], ax.x_range[2]
        y0, y1, ys = ax.y_range[0], ax.y_range[1], ax.y_range[2]
        if x_values is None:
            n = max(1, round((x1 - x0) / xs)) if xs else 1
            x_values = [x0 + i * xs for i in range(n + 1)]
        if y_values is None:
            n = max(1, round((y1 - y0) / ys)) if ys else 1
            y_values = [y0 + i * ys for i in range(n + 1)]
        for xv in x_values:
            t = T(fmt_num(xv), color=MUTED, font_size=font_size)
            t.next_to(ax.c2p(xv, y0), DOWN, buff=0.14)
            labels.add(t)
        for yv in y_values:
            t = T(fmt_num(yv), color=MUTED, font_size=font_size)
            t.next_to(ax.c2p(x0, yv), LEFT, buff=0.14)
            labels.add(t)
    except Exception:
        pass
    return labels


def axis_titles(ax: Axes, x_label: str = "", y_label: str = "",
                font_size: int = 22) -> VGroup:
    """Pango axis titles placed just outside the axes (no LaTeX)."""
    g = VGroup()
    try:
        if x_label:
            xt = T(x_label, color=PRIMARY, font_size=font_size)
            xt.next_to(ax.x_axis, DOWN, buff=0.5)
            fit(xt, ax.x_length)
            g.add(xt)
        if y_label:
            yt = T(y_label, color=PRIMARY, font_size=font_size)
            yt.rotate(PI / 2)
            yt.next_to(ax.y_axis, LEFT, buff=0.55)
            fit(yt, None, ax.y_length)
            g.add(yt)
    except Exception:
        pass
    return g


def _place_axes(scene: Scene, ax: Axes, extras: VGroup) -> None:
    """Move an axes + its labels into the working area and draw them in."""
    group = VGroup(ax, extras)
    group.move_to(ORIGIN).shift(DOWN * 0.25)
    fit(group, FRAME_W, 5.4)
    scene.play(Create(ax), run_time=1.1)
    if len(extras):
        scene.play(FadeIn(extras), run_time=0.5)


# ---------------------------------------------------------------------------
# DATA PLOTS -- line
# ---------------------------------------------------------------------------

def animated_line_plot(scene: Scene, series: Sequence[dict],
                       x_label: str = "", y_label: str = "",
                       area: str = "", show_dots: bool = True) -> VGroup:
    """
    Draw one or more line series on a shared branded ``Axes`` and animate each
    line being *drawn* left-to-right (``Create``), 3b1b-style.

    ``series`` : list of ``{"name": str, "xs": [...], "ys": [...]}``.
    Returns a VGroup of every mobject created (axes, labels, lines, dots).
    """
    col = area_color(area)
    palette = [col, ACCENT, ACCENT2, "#F472B6", "#FBBF24"]

    clean = []
    all_x: list[float] = []
    all_y: list[float] = []
    for s in series or []:
        xs = _finite(s.get("xs", []))
        ys = _finite(s.get("ys", []))
        n = min(len(xs), len(ys))
        if n >= 2:
            xs, ys = xs[:n], ys[:n]
            clean.append({"name": s.get("name", ""), "xs": xs, "ys": ys})
            all_x += xs
            all_y += ys
    if not clean:
        raise ValueError("no usable line series")

    xr = _nice_range(min(all_x), max(all_x))
    yr = _nice_range(min(all_y), max(all_y))
    ax = branded_axes(xr, yr, area=area)
    ticks = add_text_ticks(ax)
    titles = axis_titles(ax, x_label, y_label)
    extras = VGroup(ticks, titles)
    _place_axes(scene, ax, extras)

    created = VGroup(ax, extras)
    legend_rows = VGroup()
    base_y = ax.y_range[0]
    for i, s in enumerate(clean):
        c = palette[i % len(palette)]
        pts = [ax.c2p(x, y) for x, y in zip(s["xs"], s["ys"])]
        line = VMobject(color=c, stroke_width=5)
        line.set_points_as_corners(pts)

        # 3b1b "trace the curve": a ValueTracker grows a partial copy of the
        # polyline left-to-right while a dot rides its leading edge.
        traced = False
        try:
            tracker = ValueTracker(1e-3)
            drawn = VMobject(color=c, stroke_width=5)
            drawn.add_updater(lambda m, ln=line, tk=tracker:
                              m.pointwise_become_partial(ln, 0, min(1.0, max(1e-3, tk.get_value()))))
            tip = always_redraw(lambda ln=line, tk=tracker: Dot(
                ln.point_from_proportion(min(1.0, max(1e-3, tk.get_value()))),
                color=PRIMARY, radius=0.07))
            scene.add(drawn, tip)
            scene.play(tracker.animate.set_value(1.0), run_time=1.5,
                       rate_func=smooth)
            drawn.clear_updaters()
            scene.remove(tip)
            created.add(drawn)
            traced = True
        except Exception:
            scene.play(Create(line), run_time=1.3, rate_func=smooth)
            created.add(line)

        # Shade the area under the first (primary) series for depth.
        if traced and i == 0 and len(pts) >= 2:
            try:
                fill_pts = pts + [ax.c2p(s["xs"][-1], base_y),
                                  ax.c2p(s["xs"][0], base_y)]
                area = Polygon(*fill_pts, stroke_width=0,
                               fill_color=c, fill_opacity=0.13)
                scene.play(FadeIn(area), run_time=0.5)
                created.add(area)
            except Exception:
                pass

        if show_dots:
            dots = VGroup(*[Dot(p, radius=0.055, color=c) for p in pts])
            scene.play(FadeIn(dots, lag_ratio=0.15), run_time=0.6)
            created.add(dots)
            # Call out the final value of the series.
            try:
                endlab = T(fmt_num(s["ys"][-1]), color=c, font_size=20)
                endlab.next_to(pts[-1], UR, buff=0.08)
                scene.play(FadeIn(endlab), run_time=0.3)
                created.add(endlab)
            except Exception:
                pass
        if s["name"]:
            sw = Line(ORIGIN, RIGHT * 0.4, color=c, stroke_width=5)
            lab = T(s["name"], color=MUTED, font_size=20)
            lab.next_to(sw, RIGHT, buff=0.15)
            legend_rows.add(VGroup(sw, lab))
    if len(legend_rows):
        legend_rows.arrange(DOWN, aligned_edge=LEFT, buff=0.18)
        legend_rows.to_corner(UR, buff=0.6)
        scene.play(FadeIn(legend_rows), run_time=0.5)
        created.add(legend_rows)
    return created


def reveal_curve(scene: Scene, ax: Axes, func: Callable[[float], float],
                 color: str = ACCENT2, run_time: float = 2.0,
                 with_dot: bool = True) -> VGroup:
    """
    Sweep a moving dot along ``y = func(x)`` while the curve is progressively
    drawn behind it, using a ``ValueTracker`` + ``always_redraw`` (the classic
    3b1b "trace the graph" idiom). Returns the created VGroup.
    """
    x0, x1 = ax.x_range[0], ax.x_range[1]
    tracker = ValueTracker(x0)

    def _safe(x):
        try:
            y = float(func(x))
            return y if math.isfinite(y) else 0.0
        except Exception:
            return 0.0

    curve = always_redraw(lambda: ax.plot(
        _safe, x_range=[x0, tracker.get_value(), (x1 - x0) / 200.0],
        color=color, stroke_width=5))
    created = VGroup(curve)
    scene.add(curve)
    if with_dot:
        dot = always_redraw(lambda: Dot(
            ax.c2p(tracker.get_value(), _safe(tracker.get_value())),
            color=PRIMARY, radius=0.07))
        scene.add(dot)
        created.add(dot)
    scene.play(tracker.animate.set_value(x1), run_time=run_time, rate_func=smooth)
    return created


# ---------------------------------------------------------------------------
# DATA PLOTS -- scatter (great for "reproduced vs paper" agreement)
# ---------------------------------------------------------------------------

def animated_scatter(scene: Scene, xs: Sequence[float], ys: Sequence[float],
                     x_label: str = "", y_label: str = "", area: str = "",
                     diagonal: bool = False, labels: Sequence[str] | None = None
                     ) -> VGroup:
    """
    Scatter ``(xs, ys)`` as dots that pop in on a branded ``Axes``. When
    ``diagonal`` is set, a dashed ``y = x`` identity line is drawn first so the
    viewer can see how tightly the points hug it (ideal for a reproduced-vs-
    paper agreement plot). Returns the created VGroup.
    """
    col = area_color(area)
    xs = _finite(xs)
    ys = _finite(ys)
    n = min(len(xs), len(ys))
    if n < 1:
        raise ValueError("no usable scatter points")
    xs, ys = xs[:n], ys[:n]

    if diagonal:
        lo = min(min(xs), min(ys))
        hi = max(max(xs), max(ys))
        xr = _nice_range(lo, hi)
        yr = xr
    else:
        xr = _nice_range(min(xs), max(xs))
        yr = _nice_range(min(ys), max(ys))

    ax = branded_axes(xr, yr, x_length=6.4, y_length=5.0, area=area)
    ticks = add_text_ticks(ax)
    titles = axis_titles(ax, x_label, y_label)
    extras = VGroup(ticks, titles)
    _place_axes(scene, ax, extras)
    created = VGroup(ax, extras)

    if diagonal:
        p0 = ax.c2p(xr[0], xr[0])
        p1 = ax.c2p(xr[1], xr[1])
        ident = DashedLine(p0, p1, color=MUTED, stroke_width=3,
                           dash_length=0.12)
        idlab = T("y = x  (perfect match)", color=MUTED, font_size=20)
        idlab.next_to(ident.get_end(), DL, buff=0.1)
        fit(idlab, ax.x_length)
        scene.play(Create(ident), FadeIn(idlab), run_time=0.8)
        created.add(ident, idlab)

    dots = VGroup(*[Dot(ax.c2p(x, y), radius=0.07, color=col,
                        fill_opacity=0.9) for x, y in zip(xs, ys)])
    scene.play(FadeIn(dots, lag_ratio=0.08, scale=0.4), run_time=1.4)
    created.add(dots)

    # Progressive-reveal the residual of each point to the y=x identity: a short
    # segment from every dot to its projection on the diagonal. Tight hugging of
    # the line is the whole "we reproduced it" story, so make it visible.
    if diagonal and n >= 2:
        try:
            resid = VGroup()
            for x, y in zip(xs, ys):
                m = (x + y) / 2.0            # projection of (x,y) onto y=x
                seg = Line(ax.c2p(x, y), ax.c2p(m, m),
                           color=ACCENT2, stroke_width=2, stroke_opacity=0.7)
                resid.add(seg)
            scene.play(Create(resid, lag_ratio=0.04), run_time=1.2)
            created.add(resid)
            mae = sum(abs(y - x) for x, y in zip(xs, ys)) / n
            callout = T(f"mean |reproduced - paper| = {fmt_num(mae)}",
                        color=PRIMARY, font_size=22)
            fit(callout, FRAME_W)
            callout.to_edge(DOWN, buff=1.05)
            scene.play(FadeIn(callout, shift=UP * 0.1), run_time=0.5)
            created.add(callout)
        except Exception:
            pass

    # Camera flourish: push in on the densest region, then pull back so the
    # whole cloud + identity line frame the reproduction fidelity.
    zoomed = zoom_to(scene, dots, factor=0.68, run_time=1.0,
                     shift=UP * 0.1)
    try:
        scene.play(Circumscribe(dots, color=ACCENT2, run_time=1.0))
    except Exception:
        pass
    if zoomed:
        restore_camera(scene, run_time=0.9)
    return created


# ---------------------------------------------------------------------------
# DATA PLOTS -- bars
# ---------------------------------------------------------------------------

def show_bars(scene: Scene, labels: Sequence[str], values: Sequence[float],
              title: str = "", area: str = "") -> VGroup:
    """
    Single-series bar chart on a baseline (bars grow up from the axis).
    Returns the created VGroup.
    """
    col = area_color(area)
    vals = _finite(values)
    labels = list(labels)[:len(vals)]
    if not vals:
        raise ValueError("no usable bar values")

    baseline_y = -2.0
    max_h = 3.4
    n = len(vals)
    slot = min(2.6, FRAME_W / n)
    start_x = -slot * n / 2 + slot / 2
    bar_w = min(1.0, slot * 0.55)
    gmax = max(abs(v) for v in vals) or 1e-9

    base = Line([-FRAME_W / 2, baseline_y, 0], [FRAME_W / 2, baseline_y, 0],
                color=GRID, stroke_width=2)
    scene.play(Create(base), run_time=0.4)
    created = VGroup(base)

    grows, vlabels, nlabels = [], VGroup(), VGroup()
    bars = VGroup()
    for i, (lab, v) in enumerate(zip(labels, vals)):
        cx = start_x + i * slot
        h = max(0.06, abs(v) / gmax * max_h)
        bar = Rectangle(width=bar_w, height=h, stroke_width=0,
                        fill_color=col, fill_opacity=0.95)
        bar.move_to([cx, baseline_y + h / 2, 0])
        bars.add(bar)
        grows.append(GrowFromEdge(bar, DOWN))
        vl = T(fmt_num(v), color=PRIMARY, font_size=20)
        vl.next_to(bar, UP, buff=0.1)
        vlabels.add(vl)
        nm = T(str(lab), color=MUTED, font_size=20)
        fit(nm, slot * 0.94)
        nm.next_to([cx, baseline_y, 0], DOWN, buff=0.22)
        nlabels.add(nm)

    scene.play(FadeIn(nlabels), run_time=0.5)
    scene.play(*grows, run_time=1.5, rate_func=smooth, lag_ratio=0.12)
    scene.play(FadeIn(vlabels), run_time=0.5)
    created.add(bars, vlabels, nlabels)

    # Draw the eye to the leading bar (the headline number).
    try:
        top = max(range(len(vals)), key=lambda k: abs(vals[k]))
        scene.play(Indicate(bars[top], color=ACCENT2, scale_factor=1.12),
                   run_time=0.8)
    except Exception:
        pass
    return created


def grouped_bar_compare(scene: Scene, labels: Sequence[str],
                        paper_vals: Sequence[float], repro_vals: Sequence[float],
                        area: str = "", paper_name: str = "Paper",
                        repro_name: str = "Reproduced",
                        normalize: str = "global") -> VGroup:
    """
    Side-by-side (paper vs reproduced) grouped bars with a legend, value
    callouts and a shared baseline. Returns the created VGroup.

    ``normalize``: ``"global"`` scales every bar to one shared maximum;
    ``"group"`` scales each label's pair to its own local maximum (use this
    when different labels live on very different scales so small ones stay
    visible).
    """
    col = area_color(area)
    paper = _finite(paper_vals)
    repro = _finite(repro_vals)
    n = min(len(labels), len(paper), len(repro))
    if n < 1:
        raise ValueError("no usable paired values")
    labels = list(labels)[:n]
    paper, repro = paper[:n], repro[:n]

    baseline_y = -2.0
    max_h = 3.1
    slot = min(2.8, FRAME_W / n)
    start_x = -slot * n / 2 + slot / 2
    bar_w = min(0.55, slot * 0.28)
    gmax = max([abs(v) for v in paper + repro]) or 1e-9

    base = Line([-FRAME_W / 2, baseline_y, 0], [FRAME_W / 2, baseline_y, 0],
                color=GRID, stroke_width=2)
    scene.play(Create(base), run_time=0.4)
    created = VGroup(base)

    grows, vlabels, nlabels = [], VGroup(), VGroup()
    bars = VGroup()
    gap = bar_w * 0.62
    for i, (lab, pv, rv) in enumerate(zip(labels, paper, repro)):
        cx = start_x + i * slot
        denom = (max(abs(pv), abs(rv)) or 1e-9) if normalize == "group" else gmax
        ph = max(0.06, abs(pv) / denom * max_h)
        rh = max(0.06, abs(rv) / denom * max_h)
        pbar = Rectangle(width=bar_w, height=ph, stroke_width=0,
                         fill_color=ACCENT, fill_opacity=0.95)
        pbar.move_to([cx - gap, baseline_y + ph / 2, 0])
        rbar = Rectangle(width=bar_w, height=rh, stroke_width=0,
                         fill_color=col, fill_opacity=0.95)
        rbar.move_to([cx + gap, baseline_y + rh / 2, 0])
        bars.add(pbar, rbar)
        grows += [GrowFromEdge(pbar, DOWN), GrowFromEdge(rbar, DOWN)]
        pl = T(fmt_num(pv), color=ACCENT, font_size=16)
        pl.next_to(pbar, UP, buff=0.08)
        rl = T(fmt_num(rv), color=col, font_size=16)
        rl.next_to(rbar, UP, buff=0.08)
        vlabels.add(pl, rl)
        nm = T(str(lab), color=MUTED, font_size=18)
        fit(nm, slot * 0.94)
        nm.next_to([cx, baseline_y, 0], DOWN, buff=0.22)
        nlabels.add(nm)

    scene.play(FadeIn(nlabels), run_time=0.5)
    # Stagger the bar growth so each pair lands in turn (3b1b pacing).
    scene.play(*grows, run_time=1.6, rate_func=smooth, lag_ratio=0.12)
    scene.play(FadeIn(vlabels), run_time=0.5)

    legend = _legend([(paper_name, ACCENT), (repro_name, col)])
    legend.to_edge(UP, buff=0.9)
    scene.play(FadeIn(legend), run_time=0.5)
    created.add(bars, vlabels, nlabels, legend)

    # Emphasise the tightest paper-vs-reproduced agreement -- the "it matches"
    # beat -- then push the camera in on that pair briefly.
    try:
        gaps = [abs(rv - pv) / (max(abs(pv), abs(rv)) or 1e-9)
                for pv, rv in zip(paper, repro)]
        best = min(range(len(gaps)), key=lambda k: gaps[k])
        pair = VGroup(bars[2 * best], bars[2 * best + 1])
        note = T("closest match", color=ACCENT2, font_size=20)
        note.next_to(pair, UP, buff=0.65)
        zoomed = zoom_to(scene, pair, factor=0.7, run_time=0.9, shift=UP * 0.3)
        scene.play(Circumscribe(pair, color=ACCENT2, run_time=0.9),
                   FadeIn(note), run_time=0.9)
        created.add(note)
        if zoomed:
            restore_camera(scene, run_time=0.8)
    except Exception:
        pass
    return created


def _legend(entries: Sequence[tuple[str, str]]) -> VGroup:
    row = VGroup()
    for text, colr in entries:
        sw = Rectangle(width=0.3, height=0.22, stroke_width=0,
                       fill_color=colr, fill_opacity=1.0)
        lab = T(text, color=MUTED, font_size=20)
        lab.next_to(sw, RIGHT, buff=0.15)
        row.add(VGroup(sw, lab))
    row.arrange(RIGHT, buff=0.6)
    return row


# ---------------------------------------------------------------------------
# GEOMETRY + TRANSFORMS  (the "graphical flourish")
# ---------------------------------------------------------------------------

def transform_shapes(scene: Scene, area: str = "",
                     caption: str = "") -> VGroup:
    """
    A short geometric morph sequence: a Square is drawn, morphs into a
    RegularPolygon and then into a Circle -- illustrating continuity /
    transformation. Uses ``Transform`` + ``ReplacementTransform``. Returns the
    surviving VGroup.
    """
    col = area_color(area)
    shape = Square(side_length=2.2, color=col, stroke_width=6)
    shape.set_fill(col, opacity=0.12)
    shape.move_to(ORIGIN)
    scene.play(DrawBorderThenFill(shape), run_time=1.0)

    poly = RegularPolygon(n=6, color=ACCENT2, stroke_width=6)
    poly.set_fill(ACCENT2, opacity=0.12).scale(1.25).move_to(ORIGIN)
    scene.play(Transform(shape, poly), run_time=1.0, rate_func=smooth)

    circ = Circle(radius=1.35, color=ACCENT, stroke_width=6)
    circ.set_fill(ACCENT, opacity=0.12).move_to(ORIGIN)
    scene.play(Transform(shape, circ), run_time=1.0, rate_func=smooth)

    created = VGroup(shape)
    if caption:
        cap = T(caption, color=MUTED, font_size=24)
        cap.next_to(shape, DOWN, buff=0.5)
        fit(cap, FRAME_W)
        scene.play(FadeIn(cap), run_time=0.5)
        created.add(cap)
    scene.play(Indicate(shape, color=col, scale_factor=1.08), run_time=0.8)
    return created


def vector_sweep(scene: Scene, area: str = "", turns: float = 1.0) -> VGroup:
    """
    A rotating vector tracing a circle on a NumberPlane -- a compact vector /
    phase visual. Returns the created VGroup.
    """
    col = area_color(area)
    plane = NumberPlane(
        x_range=[-3, 3, 1], y_range=[-3, 3, 1],
        x_length=5.2, y_length=5.2,
        background_line_style={"stroke_color": GRID, "stroke_width": 1,
                               "stroke_opacity": 0.7},
        axis_config={"stroke_color": MUTED, "stroke_width": 2,
                     "include_numbers": False, "include_tip": False},
    ).move_to(ORIGIN)
    scene.play(Create(plane), run_time=1.0)

    tracker = ValueTracker(0.0)
    R = 2.0

    def _vec():
        a = tracker.get_value()
        return Arrow(plane.c2p(0, 0), plane.c2p(R * math.cos(a), R * math.sin(a)),
                     color=col, buff=0, stroke_width=6, max_tip_length_to_length_ratio=0.15)

    vec = always_redraw(_vec)
    tip_dot = always_redraw(lambda: Dot(
        plane.c2p(R * math.cos(tracker.get_value()), R * math.sin(tracker.get_value())),
        color=ACCENT2, radius=0.07))
    ring = Circle(radius=(plane.c2p(R, 0)[0] - plane.c2p(0, 0)[0]),
                  color=ACCENT2, stroke_width=2, stroke_opacity=0.5).move_to(plane.c2p(0, 0))
    scene.add(ring, vec, tip_dot)
    scene.play(tracker.animate.set_value(TAU * turns), run_time=2.4, rate_func=smooth)
    return VGroup(plane, ring, vec, tip_dot)


def number_line_demo(scene: Scene, values: Sequence[float], area: str = "",
                     caption: str = "") -> VGroup:
    """Plot values as ticks/dots on a horizontal NumberLine. Returns VGroup."""
    col = area_color(area)
    vals = _finite(values)
    if not vals:
        raise ValueError("no values for number line")
    lo, hi, step = _nice_range(min(vals), max(vals))
    nl = NumberLine(x_range=[lo, hi, step], length=10.0, color=MUTED,
                    include_numbers=False, include_tip=True)
    nl.move_to(ORIGIN)
    scene.play(Create(nl), run_time=1.0)
    ticks = add_number_line_labels(nl, lo, hi, step)
    scene.play(FadeIn(ticks), run_time=0.5)
    dots = VGroup(*[Dot(nl.number_to_point(v), color=col, radius=0.09) for v in vals])
    scene.play(FadeIn(dots, lag_ratio=0.1, scale=0.4), run_time=1.0)
    created = VGroup(nl, ticks, dots)
    if caption:
        cap = T(caption, color=MUTED, font_size=24)
        cap.next_to(nl, UP, buff=0.8)
        fit(cap, FRAME_W)
        scene.play(FadeIn(cap), run_time=0.5)
        created.add(cap)
    return created


def add_number_line_labels(nl: NumberLine, lo: float, hi: float, step: float,
                           font_size: int = 18) -> VGroup:
    labels = VGroup()
    try:
        n = max(1, round((hi - lo) / step)) if step else 1
        for i in range(n + 1):
            v = lo + i * step
            t = T(fmt_num(v), color=MUTED, font_size=font_size)
            t.next_to(nl.number_to_point(v), DOWN, buff=0.18)
            labels.add(t)
    except Exception:
        pass
    return labels


def conceptual_diagram(scene: Scene, nodes: Sequence[str], area: str = "",
                       title: str = "") -> VGroup:
    """
    An elegant animated node-and-arrow diagram for papers with no numeric
    series: labelled circles connected by arrows that draw in sequence, then a
    pulse travels through. Fully graphical (not just text). Returns VGroup.
    """
    col = area_color(area)
    names = [str(n) for n in (nodes or []) if str(n).strip()][:4]
    if len(names) < 2:
        names = (names + ["Input", "Model", "Result"])[:3]

    node_group = VGroup()
    for name in names:
        lab = T(name, color=PRIMARY, font_size=22)
        fit(lab, 2.0)
        r = max(lab.width, lab.height) / 2 + 0.35
        ring = Circle(radius=r, color=col, stroke_width=4)
        ring.set_fill(col, opacity=0.10)
        lab.move_to(ring.get_center())
        node_group.add(VGroup(ring, lab))
    node_group.arrange(RIGHT, buff=1.4)
    fit(node_group, FRAME_W)
    node_group.move_to(ORIGIN)

    created = VGroup()
    if title:
        ttl = T(title, weight="BOLD", color=PRIMARY, font_size=30)
        fit(ttl, FRAME_W)
        ttl.to_edge(UP, buff=0.9)
        scene.play(Write(ttl), run_time=0.7)
        created.add(ttl)

    arrows = VGroup()
    scene.play(DrawBorderThenFill(node_group[0]), run_time=0.7)
    for i in range(1, len(node_group)):
        a = node_group[i - 1]
        b = node_group[i]
        arr = Arrow(a.get_right(), b.get_left(), color=ACCENT2, buff=0.12,
                    stroke_width=5, max_tip_length_to_length_ratio=0.2)
        scene.play(GrowArrow(arr), DrawBorderThenFill(node_group[i]), run_time=0.7)
        arrows.add(arr)
    created.add(node_group, arrows)

    # a pulse travelling through the pipeline
    try:
        pulse = Dot(node_group[0].get_center(), color=ACCENT2, radius=0.1)
        path = VMobject()
        path.set_points_as_corners([n.get_center() for n in node_group])
        scene.add(pulse)
        scene.play(MoveAlongPath(pulse, path), run_time=1.6, rate_func=smooth)
        scene.play(Flash(pulse, color=col, line_length=0.3), run_time=0.6)
        scene.remove(pulse)
    except Exception:
        pass
    return created


# ---------------------------------------------------------------------------
# EMPHASIS
# ---------------------------------------------------------------------------

def highlight(scene: Scene, mobj, style: str = "indicate",
              color: str = ACCENT2) -> None:
    """Draw the eye to ``mobj`` with a 3b1b-style emphasis animation."""
    try:
        if style == "flash":
            scene.play(Flash(mobj, color=color, line_length=0.3), run_time=0.8)
        elif style == "circumscribe":
            scene.play(Circumscribe(mobj, color=color), run_time=1.0)
        else:
            scene.play(Indicate(mobj, color=color, scale_factor=1.12), run_time=0.8)
    except Exception:
        pass


__all__ = [
    "BG", "PRIMARY", "MUTED", "ACCENT", "ACCENT2", "GRID",
    "AREA_COLORS", "AREA_NAMES", "FRAME_W", "FRAME_H", "FONT",
    "area_color", "area_name", "pick_font", "T", "MT", "fit", "wrap",
    "fmt_num", "camera_frame", "save_camera_home", "zoom_to", "restore_camera",
    "backdrop", "area_chip", "title_card", "outro_card",
    "section_label", "branded_axes", "add_text_ticks", "axis_titles",
    "animated_line_plot", "reveal_curve", "animated_scatter", "show_bars",
    "grouped_bar_compare", "transform_shapes", "vector_sweep",
    "number_line_demo", "add_number_line_labels", "conceptual_diagram",
    "highlight",
]
