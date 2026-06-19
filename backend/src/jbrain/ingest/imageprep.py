"""Downscale oversized images before they reach a vision model.

A phone photo (several thousand px per side) becomes thousands of vision tokens:
slow to encode and enough to overflow a modest context window, which is what
wedged local OCR into a timeout/retry loop. Capping the long side keeps text
legible for OCR while cutting the token count sharply. Decode failures fall open
— the original bytes pass through untouched so a format Pillow can't read still
reaches the model.
"""

from __future__ import annotations

import io

import structlog
from PIL import Image, ImageOps, UnidentifiedImageError

log = structlog.get_logger()

# Long-side cap: large enough that document text stays readable, small enough
# that a 4000px photo stops producing thousands of image tokens.
MAX_SIDE = 2048
# Re-encode quality — high enough to preserve OCR legibility.
JPEG_QUALITY = 90


def downscale_for_vision(data: bytes, media_type: str) -> tuple[bytes, str]:
    """Return (possibly smaller) image bytes + media type for a vision call.

    No-op (returns the input unchanged) when the image is already within
    `MAX_SIDE` or can't be decoded."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)  # honor camera rotation before measuring
            width, height = img.size
            if max(width, height) <= MAX_SIDE:
                return data, media_type
            scale = MAX_SIDE / max(width, height)
            resized = img.resize(
                (max(1, round(width * scale)), max(1, round(height * scale))),
                Image.Resampling.LANCZOS,
            )
            if resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=JPEG_QUALITY)
    except (UnidentifiedImageError, OSError) as exc:
        log.info("vision.downscale_skipped", media_type=media_type, error=str(exc))
        return data, media_type
    return buf.getvalue(), "image/jpeg"
