"""The shared chat-image helpers (agent.chat_images): the strict image sniff and the
header-only dimension read that guards against a decompression bomb. Pure — no DB."""

import io

import pytest
from PIL import Image

from jbrain.agent import chat_images
from jbrain.agent.chat_images import (
    ImageTooLarge,
    UndecodableImage,
    image_dimensions,
    sniff_image_media_type,
    stitch_side_by_side,
)


def _jpeg(w: int = 64, h: int = 48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def test_sniff_accepts_the_web_image_formats_and_rejects_everything_else() -> None:
    assert sniff_image_media_type(b"\x89PNG\r\n\x1a\n\x00") == "image/png"
    assert sniff_image_media_type(b"\xff\xd8\xff\xe0junk") == "image/jpeg"
    assert sniff_image_media_type(b"GIF89a....") == "image/gif"
    assert sniff_image_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    # A strict allowlist (reject-on-None): an HTML error page / redirect stub is NOT png.
    assert sniff_image_media_type(b"<!doctype html><title>404</title>") is None
    assert sniff_image_media_type(b"not an image at all") is None
    assert sniff_image_media_type(b"") is None


def test_image_dimensions_reads_real_size() -> None:
    assert image_dimensions(_jpeg(80, 60)) == (80, 60)


def test_image_dimensions_rejects_a_non_image() -> None:
    with pytest.raises(UndecodableImage):
        image_dimensions(b"<html>not an image</html>")


def test_image_dimensions_rejects_a_decompression_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cap is enforced from the HEADER dimensions, before any full decode — so an image
    # declaring more pixels than the cap is refused without allocating it. Modeled by
    # lowering the cap under a small real image.
    monkeypatch.setattr(chat_images, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ImageTooLarge):
        image_dimensions(_jpeg(64, 48))  # 3072 px > 100


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_stitch_side_by_side_composes_widths_at_shared_height() -> None:
    # Two images at the same height stitch to (w1 + w2 + gap) × h — one PNG the owner sees.
    out = stitch_side_by_side([_png(40, 30), _png(60, 30)], gap=8)
    with Image.open(io.BytesIO(out)) as img:
        assert img.format == "PNG" and img.height == 30 and img.width == 40 + 60 + 8


def test_stitch_normalizes_mismatched_heights_and_skips_junk() -> None:
    # Different heights normalize to the shortest; an undecodable entry is skipped, not fatal.
    out = stitch_side_by_side([_png(40, 60), b"not an image", _png(40, 30)])
    with Image.open(io.BytesIO(out)) as img:
        assert img.height == 30  # normalized to the shortest real image
    with pytest.raises(UndecodableImage):
        stitch_side_by_side([b"junk", b"also junk"])  # none decode → clean error
