"""Draw a notice as the one 1920x1080 frame a TV is cast (JPANEL_CAST_PLAN.md K1; the mock's
"On the TV" boards, docs/mocks/jpanel-cast/).

Pure: words and picture bytes in, PNG bytes out. Three layouts, picked by what the notice has
(the PWA's composer draws its own frame instead, and `take` only holds it to the screen):

- **words** — no picture: title and text on the dark sheet, big enough to read across a room
  (the title is about 7% of the screen's width).
- **split** — a picture and text: the picture fills the left 56%, cropped to fill and never
  stretched, with the words beside it. The default whenever there is a picture and text.
- **picture** — a picture and at most a title: the picture whole on the screen (letterboxed,
  never cut), the title over a scrim along the bottom.

The sender and time sit at the top. PyMuPDF draws the text for the reason `draw/render.py`
gives: it is pinned already, antialiases, and bundles a font with the em-dash and CJK, where
the api image ships no system fonts at all.
"""

from __future__ import annotations

import io
from typing import Literal

import pymupdf
from PIL import Image, ImageOps

W, H = 1920, 1080
SPLIT_W = round(W * 0.56)
MARGIN = 120
TITLE_PX = 132
BODY_PX = 60
META_PX = 34
TITLE_LINES = 3
BODY_LINES = 7

_INK = (0.94, 0.94, 0.95)
_MUTED = (0.62, 0.63, 0.66)
_SHEET = (0.047, 0.051, 0.063)  # the mock's #0c0d10
_FONT = "dsf"

Layout = Literal["words", "split", "picture"]


class FrameError(ValueError):
    """The picture could not be read."""


def layout_for(title: str, body: str, picture: bytes | None) -> Layout:
    if picture is None:
        return "words"
    return "split" if body.strip() else "picture"


def render(title: str, body: str = "", picture: bytes | None = None, *, meta: str = "") -> bytes:
    """The frame as PNG. `meta` is the top line: who sent it and when."""
    layout = layout_for(title, body, picture)
    photo = _photo(picture) if picture is not None else None
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=W, height=H)
        page.insert_font(fontname=_FONT, fontbuffer=pymupdf.Font("cjk").buffer)
        _fill(page, pymupdf.Rect(0, 0, W, H), _SHEET)
        font = pymupdf.Font("cjk")
        if layout == "words":
            _words(page, font, title, body, meta, left=MARGIN, right=W - MARGIN)
        elif layout == "split":
            assert photo is not None
            page.insert_image(pymupdf.Rect(0, 0, SPLIT_W, H), stream=_cover(photo, SPLIT_W, H))
            _words(page, font, title, body, meta, left=SPLIT_W + 80, right=W - 90)
        else:
            assert photo is not None
            page.insert_image(pymupdf.Rect(0, 0, W, H), stream=_contain(photo, W, H))
            _caption(page, font, title, meta)
        return page.get_pixmap(alpha=False).tobytes("png")
    finally:
        doc.close()


def take(data: bytes) -> bytes:
    """A frame the phone drew itself (the composer: a photo with words placed over it), held to
    the screen: 1920x1080 as sent, else letterboxed onto the sheet, never stretched. JPEG,
    because a PNG of a photo can run past what the sidecar takes in one request."""
    img = _photo(data)
    if img.size != (W, H):
        img = Image.open(io.BytesIO(_contain(img, W, H)))
    return _jpeg(img)


def mime_of(data: bytes) -> str:
    return "image/jpeg" if data.startswith(b"\xff\xd8\xff") else "image/png"


def _photo(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as img:
            return ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - any decode failure means "not a picture"
        raise FrameError("that picture could not be read") from exc


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _cover(img: Image.Image, w: int, h: int) -> bytes:
    return _jpeg(ImageOps.fit(img, (w, h), Image.Resampling.LANCZOS))


def _contain(img: Image.Image, w: int, h: int) -> bytes:
    sheet = Image.new("RGB", (w, h), tuple(round(c * 255) for c in _SHEET))
    fitted = ImageOps.contain(img, (w, h), Image.Resampling.LANCZOS)
    sheet.paste(fitted, ((w - fitted.width) // 2, (h - fitted.height) // 2))
    return _jpeg(sheet)


def _fill(
    page: pymupdf.Page, rect: pymupdf.Rect, colour: tuple[float, ...], opacity: float = 1.0
) -> None:
    shape = page.new_shape()
    shape.draw_rect(rect)
    shape.finish(fill=colour, color=None, fill_opacity=opacity)
    shape.commit()


def wrap(font: pymupdf.Font, text: str, size: int, width: float, max_lines: int) -> list[str]:
    """Greedy word wrap to `width`, ending in an ellipsis when it runs past `max_lines`. A word
    wider than the line is broken by characters, since a URL or CJK has no spaces to break at."""
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if font.text_length(trial, fontsize=size) <= width:
                line = trial
                continue
            if line:
                lines.append(line)
            line = word
            while font.text_length(line, fontsize=size) > width:
                cut = len(line)
                while cut > 1 and font.text_length(line[:cut], fontsize=size) > width:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and font.text_length(last + "…", fontsize=size) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _text(
    page: pymupdf.Page,
    x: float,
    y: float,
    text: str,
    size: int,
    colour: tuple[float, ...],
    bold: bool = False,
) -> None:
    # Faux bold: the bundled face has one weight, and a stroke in the fill colour thickens it.
    page.insert_text(
        pymupdf.Point(x, y),
        text,
        fontsize=size,
        fontname=_FONT,
        color=colour,
        fill=colour,
        render_mode=2 if bold else 0,
        border_width=0.035 if bold else 0.05,
    )


def _words(
    page: pymupdf.Page,
    font: pymupdf.Font,
    title: str,
    body: str,
    meta: str,
    *,
    left: float,
    right: float,
) -> None:
    width = right - left
    if meta:
        _text(page, left, 110, meta, META_PX, _MUTED)
    titles = wrap(font, title, TITLE_PX, width, TITLE_LINES)
    bodies = wrap(font, body, BODY_PX, width, BODY_LINES) if body.strip() else []
    height = len(titles) * TITLE_PX * 1.12 + (40 + len(bodies) * BODY_PX * 1.35 if bodies else 0)
    # Centred on the screen, but never up into the meta line.
    y = max(190.0, (H - height) / 2) + TITLE_PX * 0.9
    for line in titles:
        _text(page, left, y, line, TITLE_PX, _INK, bold=True)
        y += TITLE_PX * 1.12
    y += 40 - TITLE_PX * 0.12
    for line in bodies:
        _text(page, left, y, line, BODY_PX, _MUTED)
        y += BODY_PX * 1.35


def _caption(page: pymupdf.Page, font: pymupdf.Font, title: str, meta: str) -> None:
    lines = wrap(font, title, 84, W - 2 * MARGIN, 2) if title.strip() else []
    if lines:
        top = H - 90 - len(lines) * 84 * 1.15 - 30
        _fill(page, pymupdf.Rect(0, top, W, H), (0, 0, 0), opacity=0.62)
        y = top + 30 + 84 * 0.9
        for line in lines:
            _text(page, MARGIN, y, line, 84, _INK, bold=True)
            y += 84 * 1.15
    if meta:
        _fill(page, pymupdf.Rect(0, 0, W, 160), (0, 0, 0), opacity=0.45)
        _text(page, MARGIN, 105, meta, META_PX, _INK)
