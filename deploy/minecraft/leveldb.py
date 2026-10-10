"""A read-only reader for the LevelDB a Bedrock world is stored in (plan §M8).

Mojang's fork of LevelDB is the stock on-disk format with zlib compression in place of
Snappy, so this is a small stdlib reader rather than a native dependency the sidecar image
would have to build. It reads every table file and the write-ahead log and keeps, for each
key, the value with the highest sequence number — enough for a map, which only reads.

It never writes and never takes LevelDB's lock, so it can read a world while BDS runs.
Table files are immutable once written; the log is appended to, and a half-written last
record is simply dropped. A table that compaction deletes mid-read is skipped (the map
tile is re-rendered next time), never an error that stops the render.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

_TABLE_MAGIC = 0xDB4775248B80FB57
_FOOTER = 48
_BLOCK_TRAILER = 5
_LOG_BLOCK = 32768
# Compression ids Mojang's LevelDB writes: none, zlib, and raw deflate (its default).
_NONE, _ZLIB, _RAW_DEFLATE = 0, 2, 4
_PUT, _DELETE = 1, 0


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _decompress(kind: int, data: bytes) -> bytes:
    if kind == _NONE:
        return data
    if kind == _RAW_DEFLATE:
        return zlib.decompress(data, -15)
    if kind == _ZLIB:
        return zlib.decompress(data)
    raise ValueError(f"unknown LevelDB block compression {kind}")


def _block(raw: bytes, offset: int, size: int) -> bytes:
    data = raw[offset : offset + size]
    kind = raw[offset + size]
    return _decompress(kind, data)


def _block_entries(block: bytes) -> Iterator[tuple[bytes, bytes]]:
    """A block's key/value pairs: prefix-compressed keys, then a restart array."""
    (restarts,) = struct.unpack_from("<I", block, len(block) - 4)
    end = len(block) - 4 - 4 * restarts
    pos, key = 0, b""
    while pos < end:
        shared, pos = _varint(block, pos)
        unshared, pos = _varint(block, pos)
        size, pos = _varint(block, pos)
        key = key[:shared] + block[pos : pos + unshared]
        pos += unshared
        yield key, block[pos : pos + size]
        pos += size


def _table(path: Path) -> Iterator[tuple[bytes, int, bytes | None]]:
    """(user key, sequence, value or None for a deletion) for every entry in a table."""
    raw = path.read_bytes()
    if len(raw) < _FOOTER:
        return
    footer = raw[-_FOOTER:]
    (magic,) = struct.unpack_from("<Q", footer, 40)
    if magic != _TABLE_MAGIC:
        raise ValueError(f"{path.name} is not a LevelDB table")
    pos = 0
    _meta_off, pos = _varint(footer, pos)
    _meta_size, pos = _varint(footer, pos)
    index_off, pos = _varint(footer, pos)
    index_size, pos = _varint(footer, pos)
    for _last_key, handle in _block_entries(_block(raw, index_off, index_size)):
        off, p = _varint(handle, 0)
        size, _ = _varint(handle, p)
        for ikey, value in _block_entries(_block(raw, off, size)):
            (trailer,) = struct.unpack_from("<Q", ikey, len(ikey) - 8)
            seq, kind = trailer >> 8, trailer & 0xFF
            yield ikey[:-8], seq, (value if kind == _PUT else None)


def _log_records(path: Path) -> Iterator[bytes]:
    """The log's logical records, reassembled from its 32 KiB physical blocks."""
    raw = path.read_bytes()
    pos, pending = 0, b""
    while pos + 7 <= len(raw):
        left = _LOG_BLOCK - pos % _LOG_BLOCK
        if left < 7:
            pos += left  # the block's tail is padding
            continue
        size, kind = struct.unpack_from("<HB", raw, pos + 4)
        data = raw[pos + 7 : pos + 7 + size]
        pos += 7 + size
        if len(data) < size:
            return  # a record still being written when we read
        if kind == 1:  # FULL
            yield data
        elif kind == 2:  # FIRST
            pending = data
        elif kind == 3:  # MIDDLE
            pending += data
        elif kind == 4:  # LAST
            yield pending + data
            pending = b""
        else:  # zero padding written ahead of a block boundary
            continue


def _log(path: Path) -> Iterator[tuple[bytes, int, bytes | None]]:
    for batch in _log_records(path):
        if len(batch) < 12:
            continue
        seq, count = struct.unpack_from("<QI", batch, 0)
        pos = 12
        try:
            for i in range(count):
                tag = batch[pos]
                pos += 1
                klen, pos = _varint(batch, pos)
                key = batch[pos : pos + klen]
                pos += klen
                if tag == _PUT:
                    vlen, pos = _varint(batch, pos)
                    yield key, seq + i, batch[pos : pos + vlen]
                    pos += vlen
                else:
                    yield key, seq + i, None
        except IndexError:
            return  # a truncated batch


def read_db(db: Path, wanted=None) -> dict[bytes, bytes]:
    """Every live key in the database (or only those `wanted(key)` accepts), newest
    write winning and deletions removing the key."""
    best: dict[bytes, tuple[int, bytes | None]] = {}
    sources = sorted(db.glob("*.ldb")) + sorted(db.glob("*.sst")) + sorted(db.glob("*.log"))
    for path in sources:
        entries = _log(path) if path.suffix == ".log" else _table(path)
        try:
            for key, seq, value in entries:
                if wanted is not None and not wanted(key):
                    continue
                have = best.get(key)
                if have is None or seq > have[0]:
                    best[key] = (seq, value)
        except FileNotFoundError:
            continue  # compacted away while we read; the next render picks it up
    return {k: v for k, (_, v) in best.items() if v is not None}
