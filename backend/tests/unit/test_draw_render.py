"""The canvas rasterizer (jbrain.draw.render) — AGENT_CANVAS_PLAN §3.

Pixel-exact assertions on a vector renderer would be brittle, so these test the
properties that actually matter and that a refactor could silently break: the output
is a real PNG at the right size, the font can draw the characters a model will
produce, marks change pixels, the photo-legibility devices are photo-only, and an
oversized canvas is bounded.
"""

from __future__ import annotations

import io

import pymupdf
import pytest
from PIL import Image

from jbrain.draw.render import MAX_RENDER_SIDE, RenderError, render_png
from jbrain.draw.scene import Background, Scene, apply_ops


def _blank(width: int = 300, height: int = 200) -> Scene:
    return Scene(canvas_id="cv", background=Background(kind="blank", width=width, height=height))


def _photo(
    width: int = 300, height: int = 200, colour: tuple[int, int, int] = (120, 120, 120)
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def _colours(png: bytes) -> set[tuple[int, int, int]]:
    return {c for _n, c in _open(png).convert("RGB").getcolors(maxcolors=1 << 24)}


# --- the basics -------------------------------------------------------------


def test_renders_a_png_at_canvas_size() -> None:
    img = _open(render_png(_blank(320, 240)))
    assert img.format == "PNG"
    assert img.size == (320, 240)


def test_blank_canvas_is_white() -> None:
    assert _colours(render_png(_blank())) == {(255, 255, 255)}


def test_a_mark_changes_pixels() -> None:
    scene, _ = apply_ops(_blank(), [{"op": "rect", "x": 20, "y": 20, "w": 100, "h": 60}])
    assert len(_colours(render_png(scene))) > 1


def test_zero_area_canvas_is_refused() -> None:
    with pytest.raises(RenderError, match="no area"):
        render_png(Scene(canvas_id="cv", background=Background(kind="blank", width=0, height=10)))


# --- fonts: the production trap this design exists to avoid -----------------


def test_the_bundled_font_has_the_glyphs_a_model_will_write() -> None:
    # `python:3.12-slim` ships NO system fonts, so anything depending on
    # fontconfig/ImageFont.truetype renders on a dev box and tofus in production.
    # PyMuPDF's bundled CJK fallback is what makes this safe — assert it directly,
    # because a silent tofu is exactly the failure that would ship unnoticed.
    font = pymupdf.Font("cjk")
    for char in "—…°′″≈日本語café→":
        assert font.has_glyph(ord(char)), f"no glyph for {char!r}"


def test_em_dash_text_renders_without_error() -> None:
    scene, _ = apply_ops(_blank(), [{"op": "text", "x": 10, "y": 60, "text": "NAS — nightly"}])
    assert len(_colours(render_png(scene))) > 1


# --- photo-only legibility devices ------------------------------------------


def _dark_pixels(png: bytes, threshold: int = 200) -> int:
    """Count near-black pixels. Darkness alone can't distinguish a backing plate from
    glyph ink — both are dark — so the discriminator is AREA: a plate behind "hello"
    is thousands of pixels, the glyph strokes are a few hundred."""
    return sum(
        n for n, c in _open(png).convert("RGB").getcolors(maxcolors=1 << 24) if sum(c) < threshold
    )


def test_text_on_a_blank_sheet_has_no_backing_plate() -> None:
    # The plate is a photo device. On white, a dark plate behind dark text is WORSE
    # contrast than none, which is what the first render of this feature looked like.
    scene, _ = apply_ops(_blank(), [{"op": "text", "x": 10, "y": 60, "text": "hello"}])
    assert _dark_pixels(render_png(scene)) < 800  # glyph ink only


def test_text_on_a_photo_gets_a_backing_plate() -> None:
    scene, _ = apply_ops(
        Scene(
            canvas_id="cv", background=Background(kind="attachment", width=300, height=200, ref="a")
        ),
        [{"op": "text", "x": 10, "y": 60, "text": "hello"}],
    )
    # Same string, same size: the difference is the plate's filled area behind it.
    assert _dark_pixels(render_png(scene, _photo())) > 1500


def test_auto_tone_differs_between_a_photo_and_a_blank_sheet() -> None:
    ops = [{"op": "rect", "x": 20, "y": 20, "w": 100, "h": 60}]
    blank, _ = apply_ops(_blank(), ops)
    photo_scene, _ = apply_ops(
        Scene(
            canvas_id="cv", background=Background(kind="attachment", width=300, height=200, ref="a")
        ),
        ops,
    )
    # auto = near-black on white, rose over a photograph.
    assert render_png(blank) != render_png(photo_scene, _photo())


# --- background compositing -------------------------------------------------


def test_photo_background_is_composited() -> None:
    png = render_png(
        Scene(
            canvas_id="cv", background=Background(kind="attachment", width=300, height=200, ref="a")
        ),
        _photo(colour=(200, 30, 30)),
    )
    r, g, b = _open(png).convert("RGB").getpixel((150, 100))
    assert r > 150 and g < 80 and b < 80


def test_exif_rotated_background_is_transposed_to_match_the_scene_frame() -> None:
    # The scene's width/height come from `image_dimensions`, which reports the
    # DISPLAYED size. Inserting the raw bytes would paint a landscape photo into a
    # portrait frame and every fractional coordinate would land on the wrong thing.
    buf = io.BytesIO()
    img = Image.new("RGB", (300, 200))
    img.paste((220, 20, 20), (0, 0, 300, 100))  # red band across the TOP when upright
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90° CW on display → displayed size is 200x300
    img.save(buf, format="JPEG", exif=exif, quality=95)

    png = render_png(
        Scene(
            canvas_id="cv", background=Background(kind="attachment", width=200, height=300, ref="a")
        ),
        buf.getvalue(),
    )
    assert _open(png).size == (200, 300)


def test_unreadable_background_is_a_clean_error() -> None:
    with pytest.raises(RenderError, match="could not be read"):
        render_png(
            Scene(
                canvas_id="cv",
                background=Background(kind="attachment", width=10, height=10, ref="a"),
            ),
            b"<html>not an image</html>",
        )


# --- bounds -----------------------------------------------------------------


def test_an_oversized_canvas_is_capped_on_the_long_edge() -> None:
    # Keeps a 12000px source from producing a 400MB PNG, and keeps the render a sane
    # vision input for the verification pass.
    png = render_png(_blank(6000, 3000))
    width, height = _open(png).size
    assert max(width, height) == MAX_RENDER_SIDE
    assert width == 2 * height  # aspect preserved


def test_a_small_canvas_is_not_upscaled() -> None:
    assert _open(render_png(_blank(100, 80))).size == (100, 80)


# --- every op kind rasterizes ----------------------------------------------


def test_all_draw_ops_render() -> None:
    scene, report = apply_ops(
        _blank(400, 300),
        [
            {"op": "text", "x": 10, "y": 30, "text": "t"},
            {"op": "line", "x": 10, "y": 50, "x2": 200, "y2": 90},
            {"op": "arrow", "x": 10, "y": 100, "x2": 200, "y2": 140},
            {"op": "rect", "x": 220, "y": 40, "w": 100, "h": 60},
            {"op": "rect", "x": 220, "y": 120, "w": 100, "h": 60, "fill": True, "tone": "info"},
            {"op": "label_box", "x": 40, "y": 180, "w": 120, "h": 80, "text": "boxed"},
            {"op": "callout", "x": 200, "y": 260, "x2": 320, "y2": 200, "text": "there"},
        ],
    )
    assert report.skipped == []
    assert len(_colours(render_png(scene))) > 3


def test_a_label_box_at_the_top_edge_keeps_its_caption_on_canvas() -> None:
    # Caption normally sits above the box; at y=0 that would render off-canvas, which
    # is a silent failure — the model would believe it labelled the box.
    scene, _ = apply_ops(
        _blank(300, 200), [{"op": "label_box", "x": 20, "y": 0, "w": 120, "h": 80, "text": "top"}]
    )
    png = render_png(scene)
    top_band = _open(png).convert("RGB").crop((0, 0, 300, 100))
    assert len({c for _n, c in top_band.getcolors(maxcolors=1 << 24)}) > 1
