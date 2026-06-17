"""Profile-image helpers (Phase-6 profile-image chain): size cap + magic-byte sniffing.

Uploaded image bytes are content-addressed in the blob store (sha256) and served back by
`FileResponse`. We never trust the client's `Content-Type`: the media type is derived from the
bytes' magic number both to reject non-images on upload and to label the response on serve.
"""

from __future__ import annotations

from pathlib import Path

# A profile image is small; cap well below the 100MB attachment limit.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def sniff_image_type(header: bytes) -> str | None:
    """The image media type implied by the leading magic bytes, or None when unrecognised."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_path(path: Path) -> str:
    """Sniff a stored blob's media type from its first bytes (octet-stream fallback)."""
    try:
        with path.open("rb") as fh:
            return sniff_image_type(fh.read(16)) or "application/octet-stream"
    except OSError:
        return "application/octet-stream"
