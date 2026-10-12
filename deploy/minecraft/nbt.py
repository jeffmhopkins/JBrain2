"""Little-endian NBT, the way Bedrock stores block entities, entities and level.dat
(plan §M23).

A whole tree is read into plain Python values: a compound is a dict, a list a list, byte
arrays bytes, int and long arrays lists of ints. Every read is bounds-checked and the
input is untrusted (a world the owner imported, or one being written while we read), so
a record that is truncated, nests too deep or claims more elements than it has bytes
for raises `NbtError` rather than allocating or recursing without end.
"""

from __future__ import annotations

import struct
from typing import Any

END, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE = 0, 1, 2, 3, 4, 5, 6
BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY = 7, 8, 9, 10, 11, 12

MAX_DEPTH = 64
MAX_BYTES = 8 << 20  # far above any one chunk's block entities
_FIXED = {BYTE: "<b", SHORT: "<h", INT: "<i", LONG: "<q", FLOAT: "<f", DOUBLE: "<d"}
_ARRAY = {INT_ARRAY: ("<i", 4), LONG_ARRAY: ("<q", 8)}


class NbtError(ValueError):
    pass


class _Reader:
    def __init__(self, buf: bytes, pos: int, max_depth: int) -> None:
        self.buf, self.pos, self.max_depth = buf, pos, max_depth

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.buf):
            raise NbtError("truncated")
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def unpack(self, fmt: str) -> Any:
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]

    def count(self, size: int) -> int:
        """An element count, refused when the bytes left can't hold that many — so a
        hostile length can't allocate or loop past the record."""
        n = self.unpack("<i")
        if n < 0 or n * size > len(self.buf) - self.pos:
            raise NbtError(f"bad length {n}")
        return n

    def string(self) -> str:
        return self.take(self.unpack("<H")).decode("utf-8", "replace")

    def payload(self, tag: int, depth: int) -> Any:
        if depth > self.max_depth:
            raise NbtError("nested too deep")
        if tag in _FIXED:
            return self.unpack(_FIXED[tag])
        if tag == STRING:
            return self.string()
        if tag == BYTE_ARRAY:
            return self.take(self.count(1))
        if tag in _ARRAY:
            fmt, size = _ARRAY[tag]
            n = self.count(size)
            return list(struct.unpack(f"<{n}{fmt[1]}", self.take(n * size)))
        if tag == LIST:
            inner = self.unpack("<b")
            # Every element but an END takes at least a byte; an END list must be empty.
            n = self.count(1)
            if inner == END:
                if n:
                    raise NbtError("a list of END tags")
                return []
            return [self.payload(inner, depth + 1) for _ in range(n)]
        if tag == COMPOUND:
            out: dict[str, Any] = {}
            while (inner := self.unpack("<b")) != END:
                name = self.string()
                out[name] = self.payload(inner, depth + 1)
            return out
        raise NbtError(f"unknown tag {tag}")


def read(buf: bytes, pos: int = 0, max_depth: int = MAX_DEPTH) -> tuple[str, Any, int]:
    """One named root tag at `pos`: (name, value, the position after it)."""
    if len(buf) > MAX_BYTES:
        raise NbtError("record too large")
    r = _Reader(buf, pos, max_depth)
    tag = r.unpack("<b")
    if tag == END:
        raise NbtError("no root tag")
    name = r.string()
    return name, r.payload(tag, 1), r.pos


def read_all(buf: bytes, max_depth: int = MAX_DEPTH) -> list[Any]:
    """Every root value in a record of back-to-back tags, as Bedrock writes a chunk's
    block entities."""
    out, pos = [], 0
    while pos < len(buf):
        _, value, pos = read(buf, pos, max_depth)
        out.append(value)
    return out
