"""The vision downscaler — keeps oversized images from flooding the model with
tokens, and falls open on anything it can't decode."""

import io

from PIL import Image

from jbrain.ingest.imageprep import MAX_SIDE, downscale_for_vision


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_large_image_is_downscaled_to_jpeg_within_cap() -> None:
    data, media_type = downscale_for_vision(_png(4000, 3000), "image/png")
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) == MAX_SIDE
        # Aspect ratio preserved (4000:3000 → 2048:1536).
        assert img.size == (MAX_SIDE, round(MAX_SIDE * 3000 / 4000))


def test_small_image_passes_through_untouched() -> None:
    original = _png(800, 600)
    data, media_type = downscale_for_vision(original, "image/png")
    assert data == original
    assert media_type == "image/png"


def test_undecodable_bytes_fall_open() -> None:
    junk = b"not an image at all"
    data, media_type = downscale_for_vision(junk, "image/jpeg")
    assert data == junk
    assert media_type == "image/jpeg"
