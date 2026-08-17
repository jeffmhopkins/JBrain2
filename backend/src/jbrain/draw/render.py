"""Render a canvas scene to PNG (docs/plans/AGENT_CANVAS_PLAN.md §3).

Pure: bytes in, bytes out. No DB, no LLM, no network.

**Why PyMuPDF and not Pillow.** There is no server-side renderer in this codebase to
extend — `charttools` emits a data-only view the frontend draws, and `image_gen.render`
is the diffusion driver — so the choice was only which already-pinned dependency to
repurpose. Pillow's `ImageDraw` cannot antialias a diagonal (verified: visibly jaggy
arrow shafts) and its bundled Aileron font renders `—` and CJK as tofu. PyMuPDF is
already pinned as a PDF reader and happens to be a complete antialiased vector engine
with bundled fonts, including a CJK fallback. Zero new dependencies, and — decisive —
zero Dockerfile change: `python:3.12-slim` ships NO system fonts, so anything relying
on `ImageFont.truetype`/fontconfig renders fine on a dev box and breaks in production.

**The one conceptual wart.** A MuPDF page is measured in PDF points, so the page is
built at exactly 1pt per pixel and rendered with an identity matrix. That makes
`Rect(x1, y1, x2, y2)` image pixels and the y-axis grow downward, matching both the
model's mental model and image convention. Worth stating because the next reader will
otherwise assume PDF's bottom-left origin.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pymupdf
from PIL import Image, ImageOps

from jbrain.draw.scene import Element, Scene

_RGB = tuple[float, float, float]

# `DESIGN.md`'s accent tokens. The model conveys MEANING (`tone`) and this owns the
# mapping, which is why a "red box" comes out as JBrain's rose rather than #FF0000
# screaming out of a near-monochrome UI.
_PALETTE: dict[str, tuple[float, float, float]] = {
    "danger": (0.812, 0.541, 0.561),  # --rose   #CF8A8F
    "warn": (0.788, 0.639, 0.416),  # --amber  #C9A36A
    "info": (0.498, 0.655, 0.788),  # --steel  #7FA7C9
    "ok": (0.561, 0.737, 0.604),  # --green  #8FBC9A
    "accent": (0.643, 0.576, 0.788),  # --violet #A493C9
    "neutral": (0.129, 0.129, 0.145),
}

# The canvas-specific deviation from DESIGN.md, recorded there in the same PR: the
# muted palette is tuned for UI chrome on a flat surface, and on a busy photograph it
# has poor contrast. So every accent stroke gets a dark outer stroke beneath it and
# every label gets a backing plate. Without these, half the annotations are invisible.
_HALO = (0.06, 0.06, 0.07)
_PLATE = (0.098, 0.098, 0.110)  # --surface
_PLATE_OPACITY = 0.85
_HALO_EXTRA_PX = 2.0

_TINT_OPACITY = 0.18  # a filled rect must not hide what it is pointing at

_ARROW_HEAD_PX = 18.0
_ARROW_HEAD_ANGLE = math.radians(26)
_PLATE_PAD_PX = 6.0
_CALLOUT_GAP_PX = 4.0

# Bound the output so a 12000px source can't produce a 400MB PNG, and so the render
# stays a sane vision input for the verification pass (matching imageprep's cap).
MAX_RENDER_SIDE = 2048


class RenderError(RuntimeError):
    """The scene could not be rasterized."""


def _font() -> pymupdf.Font:
    """Droid Sans Fallback, bundled with PyMuPDF.

    Not Base-14 `helv`: that is WinAnsi-encoded and renders an em-dash as a middot,
    which a label written by a model will hit. This has the em-dash AND CJK."""
    return pymupdf.Font("cjk")


def render_png(
    scene: Scene,
    background: bytes | None = None,
    overlays: Mapping[str, bytes] | None = None,
) -> bytes:
    """Rasterize `scene` over `background` (or a white sheet) and return PNG bytes.

    `overlays` maps an `html` element's id to the transparent PNG the htmlrender
    sidecar produced for it. Passing them in rather than fetching them keeps this
    function pure and synchronous — `draw.compose` owns the network."""
    width, height = scene.width, scene.height
    if width <= 0 or height <= 0:
        raise RenderError(f"canvas has no area: {width}x{height}")
    scale = min(1.0, MAX_RENDER_SIDE / max(width, height))
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=width, height=height)
        _paint_background(page, background, width, height)
        ctx = _Ctx(
            page=page,
            font=_font(),
            width=width,
            height=height,
            on_photo=background is not None,
            overlays=overlays or {},
        )
        for element in scene.elements:
            _draw(ctx, element)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pixmap.tobytes("png")
    finally:
        doc.close()


def _paint_background(
    page: pymupdf.Page, background: bytes | None, width: int, height: int
) -> None:
    rect = pymupdf.Rect(0, 0, width, height)
    if background is None:
        shape = page.new_shape()
        shape.draw_rect(rect)
        shape.finish(fill=(1, 1, 1), color=None)
        shape.commit()
        return
    # EXIF-normalize before compositing: `image_dimensions` reports the DISPLAYED
    # (rotated) size, so the scene's frame is the rotated one. Inserting the raw bytes
    # would paint a landscape photo into a portrait frame and every fractional
    # coordinate would land on the wrong part of the picture.
    try:
        with Image.open(io.BytesIO(background)) as img:
            oriented = ImageOps.exif_transpose(img)
            buf = io.BytesIO()
            oriented.convert("RGB").save(buf, format="JPEG", quality=92)
            data = buf.getvalue()
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not usable"
        raise RenderError("that background image could not be read") from exc
    page.insert_image(rect, stream=data, keep_proportion=False)


@dataclass(frozen=True, slots=True)
class _Ctx:
    """Everything the element painters share.

    `on_photo` is load-bearing rather than cosmetic: the halo and the backing plate
    exist to keep a muted accent legible over an arbitrary photograph. On a blank
    white sheet they are actively harmful — a dark plate behind dark text is *worse*
    contrast than no plate — so both are photo-only."""

    page: pymupdf.Page
    font: pymupdf.Font
    width: int
    height: int
    on_photo: bool
    overlays: Mapping[str, bytes]


def _colour(element: Element, on_photo: bool) -> tuple[float, float, float]:
    """Resolve a semantic tone to ink.

    "auto" means high contrast against what is underneath: rose over a photograph
    (the conventional annotation colour, and the one that reads as *danger* here),
    near-black on a blank sheet."""
    if element.tone == "auto":
        return _PALETTE["danger"] if on_photo else _PALETTE["neutral"]
    return _PALETTE.get(element.tone, _PALETTE["neutral"])


def _stroke(
    ctx: _Ctx,
    draw: Callable[[pymupdf.Shape], object],
    colour: tuple[float, float, float],
    width_px: float,
    *,
    fill: tuple[float, float, float] | None = None,
    fill_opacity: float = 1.0,
) -> None:
    """Draw a path, over a dark halo when the ground is a photograph.

    Two passes rather than one because a single stroke of the muted palette
    disappears against a mid-tone photograph, and the owner cannot ask the model to
    'use a darker red' when the palette is deliberately fixed."""
    passes: list[tuple[tuple[float, float, float], float, bool]] = []
    if ctx.on_photo:
        passes.append((_HALO, _HALO_EXTRA_PX, False))
    passes.append((colour, 0.0, True))
    for colour_pass, extra, do_fill in passes:
        shape = ctx.page.new_shape()
        draw(shape)
        shape.finish(
            color=colour_pass,
            width=width_px + extra,
            fill=fill if (do_fill and fill is not None) else None,
            fill_opacity=fill_opacity,
            stroke_opacity=1.0,
        )
        shape.commit()


def _draw(ctx: _Ctx, element: Element) -> None:
    at = element.at
    colour = _colour(element, ctx.on_photo)
    x = at.get("x", 0.0) * ctx.width
    y = at.get("y", 0.0) * ctx.height
    x2 = at.get("x2", 0.0) * ctx.width
    y2 = at.get("y2", 0.0) * ctx.height

    if element.kind == "text":
        _text(ctx, x, y, element, colour)
    elif element.kind == "line":
        _line(ctx, x, y, x2, y2, element, colour)
    elif element.kind == "arrow":
        _arrow(ctx, x, y, x2, y2, element, colour)
    elif element.kind == "rect":
        _rect(ctx, x, y, x2, y2, element, colour)
    elif element.kind == "label_box":
        _rect(ctx, x, y, x2, y2, element, colour)
        # Caption above the box, or inside its top edge when the box is at the very
        # top of the frame — a label rendered off-canvas is a silent failure.
        baseline = y - _PLATE_PAD_PX
        if baseline - element.size < 0:
            baseline = y + element.size + _PLATE_PAD_PX
        _text(ctx, x, baseline, element, colour)
    elif element.kind == "callout":
        _callout(ctx, x, y, x2, y2, element, colour)
    elif element.kind == "html":
        _html_block(ctx, x, y, x2, y2, element)


def _html_block(ctx: _Ctx, x: float, y: float, x2: float, y2: float, el: Element) -> None:
    """Paste the sidecar's transparent render of this block at its rect.

    A missing overlay draws nothing on purpose: it means the render failed, and the
    caller has already told the model so in the op report. Painting a placeholder box
    would look like a successfully drawn empty block."""
    overlay = ctx.overlays.get(el.id)
    if not overlay:
        return
    ctx.page.insert_image(
        pymupdf.Rect(x, y, x2, y2), stream=overlay, keep_proportion=False, overlay=True
    )


def _line(ctx: _Ctx, x1: float, y1: float, x2: float, y2: float, el: Element, colour: _RGB) -> None:
    _stroke(
        ctx,
        lambda s: s.draw_line(pymupdf.Point(x1, y1), pymupdf.Point(x2, y2)),
        colour,
        el.stroke,
    )


def _arrow(
    ctx: _Ctx, x1: float, y1: float, x2: float, y2: float, el: Element, colour: _RGB
) -> None:
    _line(ctx, x1, y1, x2, y2, el, colour)
    # The head is drawn here, not by the model — computing an arrowhead polygon is
    # exactly the arithmetic a weak model gets wrong and never notices.
    angle = math.atan2(y2 - y1, x2 - x1)
    size = max(_ARROW_HEAD_PX, el.stroke * 4)
    left = pymupdf.Point(
        x2 - size * math.cos(angle - _ARROW_HEAD_ANGLE),
        y2 - size * math.sin(angle - _ARROW_HEAD_ANGLE),
    )
    right = pymupdf.Point(
        x2 - size * math.cos(angle + _ARROW_HEAD_ANGLE),
        y2 - size * math.sin(angle + _ARROW_HEAD_ANGLE),
    )
    tip = pymupdf.Point(x2, y2)
    _stroke(
        ctx,
        lambda s: s.draw_polyline([left, tip, right, left]),
        colour,
        el.stroke,
        fill=colour,
    )


def _rect(ctx: _Ctx, x1: float, y1: float, x2: float, y2: float, el: Element, colour: _RGB) -> None:
    rect = pymupdf.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    _stroke(
        ctx,
        lambda s: s.draw_rect(rect),
        colour,
        el.stroke,
        fill=colour if el.fill else None,
        # A "filled" box stays a tint, never opaque: the point of drawing a box on a
        # photo is to show what is inside it.
        fill_opacity=_TINT_OPACITY if el.fill else 1.0,
    )


def _fontsize(el: Element) -> int:
    """PyMuPDF types `fontsize` as an int. Sub-pixel type size has no visible effect at
    the 8..200px the scene already bounds sizes to, so rounding at the render boundary
    is honest rather than a cast that hides a mismatch."""
    return max(1, round(el.size))


def _text(ctx: _Ctx, x: float, y: float, el: Element, colour: _RGB) -> None:
    if not el.text:
        return
    size = _fontsize(el)
    if ctx.on_photo:
        text_width = ctx.font.text_length(el.text, fontsize=size)
        plate = pymupdf.Rect(
            x - _PLATE_PAD_PX,
            y - size - _PLATE_PAD_PX * 0.5,
            x + text_width + _PLATE_PAD_PX,
            y + _PLATE_PAD_PX * 0.5,
        )
        shape = ctx.page.new_shape()
        shape.draw_rect(plate)
        shape.finish(fill=_PLATE, color=None, fill_opacity=_PLATE_OPACITY)
        shape.commit()
    writer = pymupdf.TextWriter(ctx.page.rect)
    writer.append(pymupdf.Point(x, y), el.text, font=ctx.font, fontsize=size)
    writer.write_text(ctx.page, color=colour)


def _callout(
    ctx: _Ctx, x: float, y: float, x2: float, y2: float, el: Element, colour: _RGB
) -> None:
    """A label at (x, y) with a leader line to the thing it names at (x2, y2).

    One op instead of four (line + arrowhead + plate + text), which is the whole
    reason this primitive exists: the model places two points and says a word."""
    text_width = ctx.font.text_length(el.text, fontsize=_fontsize(el))
    anchor_x = x + text_width / 2
    anchor_y = y + _CALLOUT_GAP_PX if y2 > y else y - el.size - _CALLOUT_GAP_PX
    _stroke(
        ctx,
        lambda s: s.draw_line(pymupdf.Point(anchor_x, anchor_y), pymupdf.Point(x2, y2)),
        colour,
        el.stroke,
    )
    # A dot at the target reads as "this exact thing" where a bare line end reads as
    # an unfinished stroke.
    radius = max(4.0, el.stroke * 1.5)
    _stroke(
        ctx,
        lambda s: s.draw_circle(pymupdf.Point(x2, y2), radius),
        colour,
        el.stroke,
        fill=colour,
    )
    _text(ctx, x, y, el, colour)
