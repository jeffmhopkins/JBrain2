"""The spike's test card: a 1920×1080 PNG drawn with the stdlib, so the image needs no Pillow.

What it has to prove is narrow — the Default Media Receiver shows a still the box served,
full screen, on each TV — so it is one dark field, a steel band, and a few blocky words big
enough to read from the couch. The real notice frames (docs/mocks/jpanel-cast/) are rendered
by the box's htmlrender once the plan is built; this is not that.
"""

from __future__ import annotations

import struct
import zlib

W, H = 1920, 1080
BG = (12, 13, 16)
INK = (238, 240, 242)
STEEL = (127, 167, 201)

# 5×7 glyphs, one string per row, for just the letters the card uses.
_FONT: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    " ": ("00000",) * 7,
}


def _text(canvas: list[bytearray], text: str, x0: int, y0: int, cell: int, rgb: tuple) -> None:
    x = x0
    for ch in text:
        for gy, row in enumerate(_FONT[ch]):
            for gx, bit in enumerate(row):
                if bit == "1":
                    for y in range(y0 + gy * cell, y0 + (gy + 1) * cell):
                        line = canvas[y]
                        for px in range(x + gx * cell, x + (gx + 1) * cell):
                            line[px * 3 : px * 3 + 3] = bytes(rgb)
        x += 6 * cell


def text_width(text: str, cell: int) -> int:
    return len(text) * 6 * cell - cell


def png(title: str = "JBRAIN CAST TEST") -> bytes:
    title = "".join(c for c in title.upper() if c in _FONT)
    canvas = [bytearray(bytes(BG) * W) for _ in range(H)]
    for y in range(H - 24, H):
        canvas[y][:] = bytes(STEEL) * W
    cell = 16
    while cell > 4 and text_width(title, cell) > W - 240:
        cell -= 2
    _text(canvas, title, (W - text_width(title, cell)) // 2, (H - 7 * cell) // 2, cell, INK)
    raw = b"".join(b"\x00" + bytes(row) for row in canvas)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
