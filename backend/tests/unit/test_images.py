"""The image magic-byte sniffer: the client's Content-Type is never trusted, so the media type
(and the accept/reject decision on upload) comes from the leading bytes."""

from pathlib import Path

from jbrain.api.images import sniff_image_type, sniff_path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4


def test_sniff_recognises_each_supported_type() -> None:
    assert sniff_image_type(PNG) == "image/png"
    assert sniff_image_type(JPEG) == "image/jpeg"
    assert sniff_image_type(GIF) == "image/gif"
    assert sniff_image_type(WEBP) == "image/webp"


def test_sniff_rejects_non_images() -> None:
    assert sniff_image_type(b"not an image at all") is None
    assert sniff_image_type(b"") is None
    # A RIFF container that isn't WEBP (e.g. a WAV) is not an image.
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WAVE") is None


def test_sniff_rejects_svg_and_html() -> None:
    # SVG is a script-bearing XML document, not a raster image — it must never pass the gate
    # (else a served <svg onload=…> would be stored-XSS). Neither should bare HTML/XML.
    assert sniff_image_type(b"<svg xmlns='http://www.w3.org/2000/svg'><script>") is None
    assert sniff_image_type(b"<?xml version='1.0'?><svg>") is None
    assert sniff_image_type(b"<!DOCTYPE html><html>") is None


def test_sniff_path_reads_the_header(tmp_path: Path) -> None:
    f = tmp_path / "blob"
    f.write_bytes(PNG)
    assert sniff_path(f) == "image/png"
    g = tmp_path / "other"
    g.write_bytes(b"garbage")
    assert sniff_path(g) == "application/octet-stream"
    assert sniff_path(tmp_path / "missing") == "application/octet-stream"
