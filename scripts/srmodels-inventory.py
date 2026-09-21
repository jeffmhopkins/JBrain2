#!/usr/bin/env python3
"""Print a canonical inventory of an `srmodels.bin`, so two builds can be compared.

WHY THIS EXISTS AND THE BYTES DO NOT WORK. Every other image in `firmware/dist/` is checked
byte-for-byte against a fresh build — `CONFIG_APP_REPRODUCIBLE_BUILD=y` is what makes that
possible. `srmodels.bin` cannot be: esp-sr's own packer (`model/pack_model.py`) collects the
models with `os.walk` and never sorts, so the order they land in is filesystem-dependent. Two
correct builds of the identical models differ in nearly every byte while being the same size,
because one wrote `fst` first and the other `mn7_en`.

So the check becomes content rather than layout: every model, every file inside it, and the
SHA-256 of each file's bytes, sorted. That is byte-for-byte equality modulo an ordering nobody
chose — it still catches a wrong wake word, a missing model, a truncated file or a stale
commit, which is everything the original check was for.

Format, from `pack_model.py`:
    u32 model_count
    per model:  char[32] name, u32 file_count
      per file: char[32] name, u32 offset, u32 length
"""

from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path


def _name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def inventory(data: bytes) -> list[str]:
    """One sorted line per file: `model/file sha256 length`."""
    (count,) = struct.unpack_from("I", data, 0)
    pos = 4
    lines: list[str] = []
    for _ in range(count):
        model = _name(data[pos : pos + 32])
        pos += 32
        (files,) = struct.unpack_from("I", data, pos)
        pos += 4
        for _ in range(files):
            fname = _name(data[pos : pos + 32])
            pos += 32
            offset, length = struct.unpack_from("II", data, pos)
            pos += 8
            blob = data[offset : offset + length]
            if len(blob) != length:
                raise SystemExit(f"{model}/{fname}: truncated — {len(blob)} of {length} bytes")
            lines.append(f"{model}/{fname} {hashlib.sha256(blob).hexdigest()} {length}")
    return sorted(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <srmodels.bin>", file=sys.stderr)
        return 2
    data = Path(sys.argv[1]).read_bytes()
    for line in inventory(data):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
