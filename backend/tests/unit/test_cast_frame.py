"""The frame a TV is cast: one 1920x1080 PNG whatever the notice holds, the layout picked by
what it has, and text that never runs off the screen (JPANEL_CAST_PLAN.md K1)."""

from __future__ import annotations

import io
from typing import cast

import pymupdf
import pytest
from PIL import Image

from jbrain.cast import frame as F


def _jpeg(w: int, h: int, colour: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="JPEG")
    return buf.getvalue()


def _px(img: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    return cast(tuple[int, int, int], img.getpixel((x, y)))


def _open(png: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG"
    return img.convert("RGB")


@pytest.mark.parametrize(
    ("body", "picture", "layout"),
    [
        ("", None, "words"),
        ("the text", None, "words"),
        ("the text", b"x", "split"),
        ("", b"x", "picture"),
        ("   ", b"x", "picture"),
    ],
)
def test_the_layout_is_picked_by_what_the_notice_has(
    body: str, picture: bytes | None, layout: str
) -> None:
    assert F.layout_for("Title", body, picture) == layout


def test_words_fill_the_screen_on_the_dark_sheet() -> None:
    img = _open(F.render("Dinner is ready", "Come down now", meta="Dad · via jbrain · 18:30"))
    assert img.size == (F.W, F.H)
    assert _px(img, 5, 5) == pytest.approx((12, 13, 16), abs=2)
    # Something light was drawn: the title.
    bands = cast(
        tuple[tuple[int, int], ...], img.crop((F.MARGIN, 200, F.W - F.MARGIN, 900)).getextrema()
    )
    assert min(hi for _, hi in bands) > 200


def test_split_puts_the_picture_on_the_left_cropped_to_fill() -> None:
    img = _open(F.render("Look", "at this", _jpeg(400, 1600)))
    assert img.size == (F.W, F.H)
    left = _px(img, F.SPLIT_W // 2, F.H - 5)
    right = _px(img, F.W - 10, F.H - 10)
    assert left[0] > 150 and left[1] < 90
    assert right == pytest.approx((12, 13, 16), abs=2)


def test_picture_alone_is_shown_whole_with_bars() -> None:
    img = _open(F.render("", "", _jpeg(1000, 1000, (30, 200, 30))))
    # A square on a wide screen: centre is the picture, the edges are the sheet.
    assert _px(img, F.W // 2, F.H // 2)[1] > 150
    assert _px(img, 10, F.H // 2) == pytest.approx((12, 13, 16), abs=2)


def test_picture_with_a_title_and_meta_still_renders() -> None:
    img = _open(F.render("Grandma's here", "", _jpeg(1600, 900), meta="Dad · via jbrain"))
    assert img.size == (F.W, F.H)


def test_a_picture_that_is_not_one_is_refused() -> None:
    with pytest.raises(F.FrameError):
        F.render("t", "b", b"not an image")


def test_wrap_ends_in_an_ellipsis_when_it_runs_long() -> None:
    font = pymupdf.Font("cjk")
    lines = F.wrap(font, "word " * 200, 60, 600, 3)
    assert len(lines) == 3
    assert lines[-1].endswith("…")
    assert all(font.text_length(line, fontsize=60) <= 600 for line in lines)


def test_wrap_breaks_a_word_wider_than_the_line() -> None:
    font = pymupdf.Font("cjk")
    lines = F.wrap(font, "x" * 300, 60, 400, 100)
    assert len(lines) > 1
    assert "".join(lines) == "x" * 300
    assert all(font.text_length(line, fontsize=60) <= 400 for line in lines)


def test_wrap_keeps_paragraphs_and_drops_trailing_blank_lines() -> None:
    font = pymupdf.Font("cjk")
    assert F.wrap(font, "one\ntwo\n\n", 60, 1000, 5) == ["one", "two"]
