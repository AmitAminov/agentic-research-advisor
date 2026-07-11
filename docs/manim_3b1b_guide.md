# Making 3Blue1Brown-Style Animations with Manim Community 0.19 (No LaTeX)

**Summary**: A practical, grounded reference for building 3Blue1Brown-style
scientific animations with Manim **Community Edition 0.19**, using only Pango
`Text` / `MarkupText` for all labels (no LaTeX). Every idiom below is backed by
the official docs or the 3b1b source, with URLs cited inline.

**Last updated**: 2026-07-02

---

## 0. Two different libraries called "manim"

There are two actively used engines, and they are **not** API-compatible:

- **Manim Community Edition (ManimCE)** — the community fork, versioned, packaged
  on PyPI, documented at <https://docs.manim.community>. **This guide targets
  ManimCE 0.19.** Install with `pip install manim`.
- **`3b1b/manim` (ManimGL)** — Grant Sanderson's personal engine used to make the
  actual 3Blue1Brown videos, at <https://github.com/3b1b/manim>, with the scene
  code for the videos at <https://github.com/3b1b/videos>.

The **aesthetic** is shared, but the **API differs**. Where a 3b1b idiom needs
translating, this guide gives you the ManimCE spelling and notes the ManimGL
original.

| Concept | ManimGL (3b1b) | ManimCE 0.19 (use this) |
|---|---|---|
| Draw/creation anim | `ShowCreation(m)` | `Create(m)` |
| Camera object | `self.camera.frame` / `self.frame` | `self.camera.frame` (needs `MovingCameraScene`) |
| Rotate camera angles | `frame.set_euler_angles(...)` | `self.set_camera_orientation(phi=, theta=)` (in `ThreeDScene`) |
| Keep a value pinned | `f_always(dot.move_to, ...)` | `dot.add_updater(lambda m: ...)` |
| Tex matching | `TransformMatchingTex` | `TransformMatchingShapes` (no LaTeX) |
| Old Tex constructor | `OldTex("A^2")` | avoid — use `Text` (see below) |

Sources: [3b1b/manim](https://github.com/3b1b/manim),
[3b1b/videos](https://github.com/3b1b/videos),
[3b1b example scenes](https://3b1b.github.io/manim/getting_started/example_scenes.html),
[ManimCE MovingCameraScene example](https://docs.manim.community/en/stable/examples.html).

---

## 1. The hard rule: Text/MarkupText only, never LaTeX

On this target machine LaTeX may be **absent**, so `Tex` and `MathTex` will fail
to render. The community docs themselves say: *"If you want to render simple
text, you should use either `Text` or `MarkupText` … rather than LaTeX."*
(<https://docs.manim.community/en/stable/guides/using_text.html>)

`Text` is rendered through **Pango**, needs no LaTeX install, and supports fonts,
weights, colors, gradients, and per-character coloring.

```python
from manim import *

class TextBasics(Scene):
    def construct(self):
        title  = Text("Gradient Descent", font_size=56, weight=BOLD)
        sub    = Text("one idea at a time", font_size=32, slant=ITALIC)
        col    = Text("RED COLOR", color=RED)
        grad   = Text("energy", gradient=(BLUE_B, TEAL, GREEN), font_size=72)
        # per-substring coloring with t2c (text-to-color):
        t2c    = Text("loss goes down", t2c={"loss": YELLOW, "down": GREEN})
        # MarkupText for inline span styling (Pango markup, still no LaTeX):
        markup = MarkupText(f'all red <span fgcolor="{YELLOW}">except this</span>',
                            color=RED)

        VGroup(title, sub, col, grad, t2c, markup).arrange(DOWN, buff=0.35)
        self.add(title)
```

**Writing math without LaTeX.** For simple formulas, Unicode + `Text` is enough:
`Text("y = a·x + b")`, `Text("σ(x) = 1 / (1 + e⁻ˣ)")`, `Text("∑ xᵢ")`,
`Text("∂L/∂w")`. Pick a font with good glyph coverage
(`font="DejaVu Sans"` or `"Noto Sans"`). Reserve stacked fractions / integrals
for a still image if you truly need them — do not reach for `MathTex`.

Source: [Rendering Text and Formulas](https://docs.manim.community/en/stable/guides/using_text.html).

---

## 2. Scene skeleton and the core verbs

Almost all animation code lives in the `construct()` method of a `Scene`.
(<https://docs.manim.community/en/stable/tutorials/quickstart.html>)

```python
from manim import *

class SquareAndCircle(Scene):
    def construct(self):
        circle = Circle().set_fill(PINK, opacity=0.5)
        square = Square().set_fill(BLUE, opacity=0.5)
        square.next_to(circle, RIGHT, buff=0.5)   # spatial layout
        self.play(Create(circle), Create(square)) # animate
        self.wait()                                 # hold the frame
```

Core scene methods (from the Quickstart):

- `self.play(*animations, run_time=..., rate_func=...)` — run animations.
- `self.wait(duration)` — hold the current frame.
- `self.add(*mobjects)` / `self.remove(...)` — place/remove without animating.

Core creation/transition animations:

- `Create(m)` — progressively draw (ManimGL calls this `ShowCreation`).
- `Write(m)` — hand-writing reveal, ideal for `Text`.
- `FadeIn(m, shift=UP)` / `FadeOut(m)` — fade, optionally with a directional drift.
- `DrawBorderThenFill(m)` — outline then flood the fill.
- `GrowFromEdge(m, DOWN)` / `GrowFromCenter(m)` — scale-in growth (great for bars).

The **`.animate`** syntax turns any mutator into an animation:

```python
self.play(square.animate.rotate(PI / 4))
self.play(square.animate.set_fill(PINK, opacity=0.5))
```

Layout helpers you will use constantly: `.next_to(other, DIR, buff=)`,
`.shift(vec)`, `.to_edge(UP)`, `.to_corner(UL)`, `.move_to(point)`, `.arrange(DOWN)`,
and `VGroup(a, b, c)` to treat several mobjects as one.

Source: [Quickstart](https://docs.manim.community/en/stable/tutorials/quickstart.html),
[Building blocks](https://docs.manim.community/en/stable/tutorials/building_blocks.html).

---

## 3. Axes, and placing data with `c2p`

`Axes` is the workhorse for scientific plots. The single most important method is
`c2p` (alias for `coords_to_point`): it maps **data coordinates → scene points**,
so everything you draw lands in axis space regardless of the axis scale/position.

```python
class AxesAndPlot(Scene):
    def construct(self):
        ax = Axes(
            x_range=[0, 10, 1],
            y_range=[0, 5, 1],
            x_length=10, y_length=5,
            axis_config={"include_numbers": True},  # numeric ticks, no LaTeX
            tips=True,
        )
        # Text labels (NOT MathTex) — pass Text objects explicitly:
        labels = ax.get_axis_labels(
            Text("time (s)").scale(0.6),
            Text("value").scale(0.6),
        )
        curve = ax.plot(lambda x: 0.4 * x, color=BLUE)          # a function
        self.play(Create(ax), Write(labels))
        self.play(Create(curve))
```

`ax.get_axis_labels(...)` defaults to `MathTex` when you hand it **strings**, so
**always pass `Text(...)` objects** to stay LaTeX-free. For finer control use
`ax.get_x_axis_label(Text("x"))` and `ax.get_y_axis_label(Text("y"))`.

Placing a single data point:

```python
dot = Dot(ax.c2p(4, 3), color=YELLOW)   # (4,3) in DATA coords -> scene point
```

Source: [Axes reference](https://docs.manim.community/en/stable/reference/manim.mobject.graphing.coordinate_systems.Axes.html).

---

## 4. Plotting data: line, scatter, and bars

### 4a. Line graph from raw data

```python
class LineData(Scene):
    def construct(self):
        ax = Axes(x_range=[0, 7, 1], y_range=[0, 5, 1],
                  axis_config={"include_numbers": True})
        line_graph = ax.plot_line_graph(
            x_values=[0, 1.5, 2, 2.8, 4, 6.25],
            y_values=[1, 3, 2.25, 4, 2.5, 1.75],
            line_color=GOLD,
            add_vertex_dots=True,
            vertex_dot_radius=0.08,
        )
        self.play(Create(ax))
        self.play(Create(line_graph))
```

`plot_line_graph` returns a `VDict` containing `"line_graph"` and (if enabled)
`"vertex_dots"`, so you can animate the line first and pop the dots after.

### 4b. Scatter plot — `Dot` at each `c2p`

There is no dedicated scatter primitive; the idiom is a `VGroup` of `Dot`s placed
via `c2p`, revealed with a small `lag_ratio` so they appear in sequence.

```python
class Scatter(Scene):
    def construct(self):
        ax = Axes(x_range=[0, 10, 1], y_range=[0, 10, 1],
                  axis_config={"include_numbers": True})
        xs = [1, 2, 3, 4, 5, 6, 7, 8]
        ys = [2, 3.1, 2.8, 4.2, 5.0, 5.3, 6.8, 7.1]
        dots = VGroup(*[
            Dot(ax.c2p(x, y), radius=0.06, color=TEAL)
            for x, y in zip(xs, ys)
        ])
        self.play(Create(ax))
        self.play(LaggedStart(*[FadeIn(d, scale=0.5) for d in dots],
                              lag_ratio=0.1))
```

### 4c. Bar chart

Two routes. The **built-in `BarChart`** (a `Scene`-agnostic mobject) is quickest;
its `bar_names` render as plain text so it is LaTeX-safe:

```python
class Bars(Scene):
    def construct(self):
        chart = BarChart(
            values=[3, 7, 2, 5, 6],
            bar_names=["a", "b", "c", "d", "e"],
            y_range=[0, 8, 2],
            y_length=5, x_length=8,
            bar_colors=[BLUE, TEAL, GREEN, YELLOW, RED],
        )
        self.play(Create(chart))
```

For a hand-built, fully controlled bar chart, draw `Rectangle`s and grow them from
the axis with `GrowFromEdge(bar, DOWN)` — this reads as "the bar rises out of the
axis" and is a classic 3b1b move:

```python
class HandBars(Scene):
    def construct(self):
        ax = Axes(x_range=[0, 6, 1], y_range=[0, 8, 2],
                  axis_config={"include_numbers": True})
        vals = [3, 7, 2, 5, 6]
        bars = VGroup()
        for i, v in enumerate(vals, start=1):
            base = ax.c2p(i, 0)
            top  = ax.c2p(i, v)
            bar  = Rectangle(width=0.5,
                             height=(top[1] - base[1]),
                             fill_opacity=0.8, color=BLUE, stroke_width=0)
            bar.move_to(base, aligned_edge=DOWN)  # sit on the axis
            bars.add(bar)
        self.play(Create(ax))
        self.play(LaggedStart(*[GrowFromEdge(b, DOWN) for b in bars],
                              lag_ratio=0.15))
```

Source: [Axes.plot_line_graph](https://docs.manim.community/en/stable/reference/manim.mobject.graphing.coordinate_systems.Axes.html),
[Example Gallery](https://docs.manim.community/en/stable/examples.html).

---

## 5. ValueTracker + `always_redraw`: the sweep idiom

This is the signature 3b1b move: a single scalar drives the whole scene. Animate a
`ValueTracker`, and rebuild dependent mobjects each frame with `always_redraw`
(or `add_updater`). The docs' `PolygonOnAxes` example is the canonical form.

```python
class SweepDotAlongCurve(Scene):
    def construct(self):
        ax = Axes(x_range=[0, 10, 1], y_range=[0, 10, 1],
                  x_length=6, y_length=6)
        graph = ax.plot(lambda x: 25 / x, x_range=[2.5, 10], color=YELLOW_D)

        t = ValueTracker(3)   # the single source of truth

        # Dot pinned to the curve at x = t:
        dot = always_redraw(
            lambda: Dot(ax.c2p(t.get_value(), 25 / t.get_value()), color=RED)
        )
        # A vertical guide line that follows too:
        vline = always_redraw(
            lambda: ax.get_vertical_line(
                ax.c2p(t.get_value(), 25 / t.get_value()), color=GREY_B)
        )
        # A live numeric readout of t (Text-based, no LaTeX):
        readout = always_redraw(
            lambda: Text(f"x = {t.get_value():.2f}", font_size=28)
                    .to_corner(UL)
        )

        self.add(ax, graph, dot, vline, readout)
        self.play(t.animate.set_value(10), run_time=4, rate_func=smooth)
```

Equivalent with an explicit updater (closer to a general 3b1b pattern; ManimGL
often writes `f_always(dot.move_to, ...)`):

```python
dot = Dot(color=RED)
dot.add_updater(lambda m: m.move_to(ax.c2p(t.get_value(), 25 / t.get_value())))
self.add(dot)
self.play(t.animate.set_value(10), run_time=4)
dot.clear_updaters()   # stop tracking when the sweep ends
```

- Use **`always_redraw(fn)`** when the mobject is cheap to rebuild wholesale
  (lines, braces, freshly positioned text).
- Use **`add_updater`** when you want to mutate an existing mobject in place.
- Always **`clear_updaters()`** (or `remove_updater`) once the sweep is done, or
  later animations will fight the updater.

Source: [PolygonOnAxes / MovingCameraScene examples](https://docs.manim.community/en/stable/examples.html),
[Building blocks](https://docs.manim.community/en/stable/tutorials/building_blocks.html),
[3b1b example scenes](https://3b1b.github.io/manim/getting_started/example_scenes.html).

---

## 6. Morphing shapes to convey relationships

Transforms are how you *argue visually* that A becomes / equals / relates to B.

- `Transform(a, b)` — morph `a`'s points into `b`'s; **`a` stays in the scene**
  (its identity persists, now looking like `b`).
- `ReplacementTransform(a, b)` — `a` is replaced by `b`; use when `b` is the object
  you keep working with afterward.
- `TransformMatchingShapes(a, b)` — match sub-pieces by shape and morph piece by
  piece. This is the **LaTeX-free** counterpart to 3b1b's `TransformMatchingTex`,
  and it works beautifully on `Text`.

```python
class Morphs(Scene):
    def construct(self):
        sin_graph  = FunctionGraph(lambda x: np.sin(x), x_range=[-PI, PI])
        relu_graph = FunctionGraph(lambda x: max(0, x), x_range=[-PI, PI])
        self.play(Create(sin_graph))
        self.play(ReplacementTransform(sin_graph, relu_graph))  # curve -> curve

        src = Text("the morse code")
        dst = Text("here come dots")
        self.play(Write(src))
        self.play(TransformMatchingShapes(src, dst, path_arc=PI / 2))
```

A square deforming into a circle to show "same area" or "same object, new frame":

```python
sq = Square(color=BLUE)
ci = Circle(color=BLUE).move_to(sq)
self.play(Transform(sq, ci))   # sq is still the handle; it now looks like ci
```

Source: [TransformMatchingShapes](https://docs.manim.community/en/stable/reference/manim.animation.transform_matching_parts.TransformMatchingShapes.html),
[3b1b example scenes](https://3b1b.github.io/manim/getting_started/example_scenes.html).

---

## 7. Highlighting for emphasis

Short, punchy animations that draw the eye to *one* thing. Keep them under ~1s.

```python
class Highlights(Scene):
    def construct(self):
        eq  = Text("y = a·x + b", font_size=48)
        dot = Dot(RIGHT * 2, color=YELLOW)
        self.add(eq, dot)

        self.play(Indicate(eq))                 # brief scale + recolor pulse
        self.play(Flash(dot))                   # radial spark from a point
        self.play(Circumscribe(eq, color=TEAL)) # temporary box drawn around it
        self.play(Wiggle(eq))                   # playful shake
        self.play(ShowPassingFlash(Underline(eq)))  # sliver of an underline sweeps
        self.play(ApplyWave(eq))                # wave distortion
```

All follow the pattern `self.play(AnimationClass(target, run_time=..., ...))`.
Use them sparingly — one highlight per beat.

Source: [Indication animations](https://docs.manim.community/en/stable/reference/manim.animation.indication.html).

---

## 8. Number lines, vectors, arrows

```python
class LineAndVectors(Scene):
    def construct(self):
        nl = NumberLine(x_range=[-5, 5, 1], length=10, include_numbers=True)
        self.play(Create(nl))
        p = Dot(nl.number_to_point(2), color=YELLOW)  # data value -> point
        self.play(FadeIn(p))

        # Arrow between two scene points; Vector is an arrow from the origin:
        arr = Arrow(nl.number_to_point(-3), nl.number_to_point(3), buff=0,
                    color=BLUE)
        vec = Vector([2, 1], color=GREEN)
        self.play(GrowArrow(arr))
        self.play(GrowArrow(vec))
```

### Vector fields

`ArrowVectorField` takes a function of position `pos` (a numpy array) and returns
the field vector there.

```python
class Field(Scene):
    def construct(self):
        func = lambda pos: np.array([-pos[1], pos[0], 0]) / 3   # rotational field
        field = ArrowVectorField(func, x_range=[-5, 5, 1], y_range=[-3, 3, 1],
                                 colors=[BLUE, TEAL, GREEN, YELLOW])
        self.play(Create(field))
        # Streamlines animate flow along the field:
        stream = StreamLines(func, stroke_width=2, max_anchors_per_line=30)
        self.add(stream)
        stream.start_animation(warm_up=True, flow_speed=1.5)
```

Source: [ArrowVectorField](https://docs.manim.community/en/stable/reference/manim.mobject.vector_field.ArrowVectorField.html).

---

## 9. 3D basics and camera choreography

### 3D scenes

`ThreeDScene` gives you `set_camera_orientation` and `move_camera` in spherical
coordinates (`phi` = polar angle from +z, `theta` = azimuth). 2D overlays (titles)
must be pinned with `add_fixed_in_frame_mobjects` so they do not rotate.

```python
class Surface3D(ThreeDScene):
    def construct(self):
        axes = ThreeDAxes(x_range=[-3, 3, 1], y_range=[-3, 3, 1], z_range=[-3, 3, 1])
        self.set_camera_orientation(phi=70 * DEGREES, theta=-45 * DEGREES, zoom=0.9)

        title = Text("f(x, y) = x² − y²", font_size=32)
        self.add_fixed_in_frame_mobjects(title)   # stays flat on screen
        title.to_corner(UL)

        surf = Surface(
            lambda u, v: axes.c2p(u, v, u**2 - v**2),
            u_range=[-2, 2], v_range=[-2, 2],
            resolution=(24, 24), fill_opacity=0.7,
        )
        self.play(Create(axes))
        self.play(Create(surf))
        self.move_camera(phi=55 * DEGREES, theta=30 * DEGREES, run_time=3)
        self.begin_ambient_camera_rotation(rate=0.1)  # slow turntable
        self.wait(4)
        self.stop_ambient_camera_rotation()
```

### 2D camera moves (`MovingCameraScene`)

For 2D "push in / follow" moves, subclass `MovingCameraScene` and animate
`self.camera.frame`. (In ManimGL this is just `self.camera.frame` / `self.frame`
in any scene.)

```python
class PushIn(MovingCameraScene):
    def construct(self):
        self.camera.frame.save_state()
        ax = Axes(x_range=[-1, 10], y_range=[-1, 10])
        graph = ax.plot(lambda x: np.sin(x), x_range=[0, 3 * PI], color=BLUE)
        moving = Dot(ax.i2gp(0, graph), color=ORANGE)
        self.add(ax, graph, moving)

        self.play(self.camera.frame.animate.scale(0.5).move_to(moving))  # zoom in
        self.camera.frame.add_updater(lambda m: m.move_to(moving.get_center()))
        self.play(MoveAlongPath(moving, graph, rate_func=linear, run_time=4))
        self.camera.frame.clear_updaters()
        self.play(Restore(self.camera.frame))   # pull back out
```

Source: [ThreeDScene](https://docs.manim.community/en/stable/reference/manim.scene.three_d_scene.ThreeDScene.html),
[MovingCameraScene example](https://docs.manim.community/en/stable/examples.html).

---

## 10. Timing, easing, and rate functions

`run_time` sets the duration; `rate_func` sets the *feel* of the motion. The
default is `smooth` (ease-in-out), which is why Manim looks polished out of the box.

```python
self.play(square.animate.shift(UP), run_time=2, rate_func=smooth)
self.play(dot.animate.shift(RIGHT), rate_func=there_and_back)  # go and return
self.play(Count(num, 0, 100), run_time=4, rate_func=linear)    # constant speed
```

How to pass them (this trips people up):

- The common ones are exported names — use them bare: `rate_func=smooth`,
  `rate_func=linear`, `rate_func=there_and_back`, `rate_func=rush_into`,
  `rate_func=rush_from`, `rate_func=there_and_back_with_pause`, `rate_func=wiggle`.
- The named easing family is **not** exported at top level; qualify it:
  `rate_func=rate_functions.ease_in_out_sine`, `rate_functions.ease_out_cubic`,
  `rate_functions.ease_in_expo`, `rate_functions.ease_out_bounce`, etc.

The three easing shapes: **ease-in** (smooth start), **ease-out** (smooth end),
**ease-in-out** (both). Families available: `sine, quad, cubic, quart, quint, circ,
expo, back, elastic, bounce`.

Sequencing multiple animations:

```python
# Stagger a group with a delay between each start:
self.play(LaggedStart(*[FadeIn(d) for d in dots], lag_ratio=0.1))
# Play strictly one after another:
self.play(Succession(Create(a), Create(b), Create(c)))
# Fine-grained overlap:
self.play(AnimationGroup(Create(a), Write(b), lag_ratio=0.5))
```

Source: [rate_functions](https://docs.manim.community/en/stable/reference/manim.utils.rate_functions.html),
[Building blocks](https://docs.manim.community/en/stable/tutorials/building_blocks.html).

---

## 11. Title and outro card composition

Compose cards from `Text`/`MarkupText` in a `VGroup`, arrange, then reveal
progressively. A thin accent line under a title is a cheap, elegant 3b1b touch.

```python
class TitleCard(Scene):
    def construct(self):
        title = Text("Gradient Descent", font_size=64, weight=BOLD)
        rule  = Line(LEFT, RIGHT, color=BLUE).set_width(title.width)
        sub   = Text("why the loss goes downhill", font_size=30,
                     slant=ITALIC, color=GREY_B)
        card  = VGroup(title, rule, sub).arrange(DOWN, buff=0.3)

        self.play(Write(title))
        self.play(Create(rule), FadeIn(sub, shift=UP * 0.3))
        self.wait()
        self.play(FadeOut(card, shift=UP * 0.5))   # clean exit

class OutroCard(Scene):
    def construct(self):
        thanks = Text("Thanks for watching", font_size=48)
        note   = Text("built with Manim Community 0.19", font_size=24,
                      color=GREY_B)
        VGroup(thanks, note).arrange(DOWN, buff=0.4)
        self.play(FadeIn(thanks, scale=1.1))
        self.play(FadeIn(note))
        self.wait(2)
        self.play(FadeOut(thanks), FadeOut(note))
```

---

## 12. Staging principles — the 3b1b aesthetic

These are the through-lines behind the idioms above. They are what make a
scientific animation feel *considered* rather than busy.

1. **Progressive reveal — show, don't tell.** Never drop a finished diagram on
   screen. Build it in the order a person would reason about it: axes → curve →
   the one point that matters → the label. `LaggedStart` and `Succession` are your
   pacing tools; `self.wait()` gives ideas time to land.
2. **One idea at a time.** Each `self.play(...)` beat should advance exactly one
   thought. If two things change at once, the viewer misses both. Fade out what is
   no longer relevant before introducing the next idea.
3. **Motion with purpose.** Every movement should *mean* something: a dot sweeping
   a curve traces a relationship; a morph asserts an equivalence; a camera push
   says "look here." Decorative motion is noise — cut it.
4. **Ease everything.** Objects should accelerate and decelerate, never start or
   stop abruptly. The default `smooth` rate function is the baseline; reach for
   `there_and_back`, `rush_from`, or the `ease_*` family only to add specific
   character.
5. **Color discipline.** A dark background (Manim's default `#000000`-ish) with a
   restrained palette: one neutral (white/grey text), one or two accent hues
   (blues/teals for structure, yellow/red for the thing under focus). Use color to
   encode *meaning*, not decoration — the highlight color should be reserved for
   whatever you are currently pointing at.
6. **Spatial memory / persistent layout.** Objects keep their place across beats so
   the viewer builds a mental map. When something moves, animate the move (via
   `Transform` or `.animate`) rather than cutting, so the eye can follow identity
   through the change.
7. **Text is quiet.** Labels are small, secondary, and only appear when needed.
   The geometry is the star; text annotates it.

Sources (aesthetic distilled from the actual video code):
[3b1b/videos](https://github.com/3b1b/videos),
[3b1b example scenes](https://3b1b.github.io/manim/getting_started/example_scenes.html).

---

## 13. From reproduced numbers to a scene — a data-driven pipeline

When you have real numbers to visualize, choose the encoding from the data's
shape, then add exactly one geometric/transformation flourish for emphasis.

**Step 1 — pick the chart from the data relationship:**

| Data shape | Use | Manim idiom |
|---|---|---|
| One continuous quantity vs. an ordered axis (time, x) | **Line** | `ax.plot(...)` or `ax.plot_line_graph(...)` (§4a) |
| Two variables, look for correlation/cluster/spread | **Scatter** | `VGroup` of `Dot(ax.c2p(x,y))` (§4b) |
| A quantity across a few discrete categories | **Bar** | `BarChart` or `GrowFromEdge` rectangles (§4c) |
| A field / flow over space | **Vector field** | `ArrowVectorField` / `StreamLines` (§8) |
| A relationship over 3 variables / a surface | **3D** | `ThreeDAxes` + `Surface` (§9) |

**Step 2 — always place data through `c2p`.** Compute your points in *data*
coordinates and let `ax.c2p(x, y)` map them to the screen. Your code then reads in
the units of the problem, and rescaling the axes never breaks placement.

**Step 3 — stage the reveal (per §12).** Draw axes first (`Create`), add `Text`
labels (`Write`), then bring in the data with a small `lag_ratio` so the eye
follows the sequence.

**Step 4 — add one flourish that carries the argument:**

- *Trend*: sweep a `ValueTracker` dot along the plotted line with a live `Text`
  readout of the value (§5) — turns a static line into a story.
- *Fit / model*: `ReplacementTransform` the scatter's implied shape into the fitted
  line, or morph a rough guess curve into the optimum (§6).
- *Comparison*: grow bars with `GrowFromEdge` and `Indicate` / `Circumscribe` the
  winning bar (§4c, §7).
- *Emphasis on a datum*: `Flash` or `Circumscribe` the single point that matters,
  then push the camera toward it with `MovingCameraScene` (§9).

**Worked mini-example — scatter, then reveal a trend line, then sweep it:**

```python
class DataStory(Scene):
    def construct(self):
        ax = Axes(x_range=[0, 9, 1], y_range=[0, 9, 1],
                  axis_config={"include_numbers": True})
        labels = ax.get_axis_labels(Text("x").scale(0.6), Text("y").scale(0.6))

        xs = [1, 2, 3, 4, 5, 6, 7, 8]
        ys = [1.2, 2.4, 2.9, 4.1, 4.8, 6.2, 6.6, 7.9]
        dots = VGroup(*[Dot(ax.c2p(x, y), radius=0.06, color=TEAL)
                        for x, y in zip(xs, ys)])

        # 1) axes + labels
        self.play(Create(ax), Write(labels))
        # 2) scatter, sequenced
        self.play(LaggedStart(*[FadeIn(d, scale=0.5) for d in dots],
                              lag_ratio=0.1))
        # 3) reveal the fit line (slope ~0.95, intercept ~0.3)
        fit = ax.plot(lambda x: 0.95 * x + 0.3, x_range=[0, 8], color=YELLOW)
        self.play(Create(fit))
        # 4) flourish: sweep a labelled dot along the fit
        t = ValueTracker(0)
        tracer = always_redraw(
            lambda: Dot(ax.c2p(t.get_value(), 0.95 * t.get_value() + 0.3),
                        color=RED))
        readout = always_redraw(
            lambda: Text(f"ŷ = {0.95 * t.get_value() + 0.3:.1f}",
                         font_size=26).to_corner(UR))
        self.add(tracer, readout)
        self.play(t.animate.set_value(8), run_time=3, rate_func=smooth)
        self.play(Circumscribe(fit, color=YELLOW))
```

---

## 14. Rendering

```bash
# quick preview (low quality, opens the file):
manim -pql scene.py DataStory
# final (high quality 1080p60):
manim -qh scene.py DataStory
```

`-p` preview, `-q` quality (`l`/`m`/`h`/`k`), one flag per scene class name.

---

## Sources used

- Manim Community — Quickstart: <https://docs.manim.community/en/stable/tutorials/quickstart.html>
- Manim Community — Building blocks: <https://docs.manim.community/en/stable/tutorials/building_blocks.html>
- Manim Community — Rendering Text and Formulas (Text/MarkupText, no LaTeX): <https://docs.manim.community/en/stable/guides/using_text.html>
- Manim Community — Axes / plot_line_graph / c2p / get_axis_labels: <https://docs.manim.community/en/stable/reference/manim.mobject.graphing.coordinate_systems.Axes.html>
- Manim Community — Example Gallery (ValueTracker, MovingCameraScene): <https://docs.manim.community/en/stable/examples.html>
- Manim Community — Indication animations: <https://docs.manim.community/en/stable/reference/manim.animation.indication.html>
- Manim Community — TransformMatchingShapes: <https://docs.manim.community/en/stable/reference/manim.animation.transform_matching_parts.TransformMatchingShapes.html>
- Manim Community — rate_functions: <https://docs.manim.community/en/stable/reference/manim.utils.rate_functions.html>
- Manim Community — ThreeDScene: <https://docs.manim.community/en/stable/reference/manim.scene.three_d_scene.ThreeDScene.html>
- Manim Community — ArrowVectorField: <https://docs.manim.community/en/stable/reference/manim.mobject.vector_field.ArrowVectorField.html>
- 3b1b/manim (ManimGL engine): <https://github.com/3b1b/manim>
- 3b1b/videos (actual video scene code): <https://github.com/3b1b/videos>
- 3b1b example scenes (staging patterns, camera frame, updaters): <https://3b1b.github.io/manim/getting_started/example_scenes.html>
