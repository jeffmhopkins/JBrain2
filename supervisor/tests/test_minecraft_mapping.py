"""The map renderer (plan §M8): a stdlib LevelDB reader and biome tiles from `Data3D`.

The fixtures write a real LevelDB on disk — a table file compressed the way Mojang's
fork does (raw deflate) and a write-ahead log — so the reader is tested against the
format rather than against itself. The chunk layout pinned here (heights indexed
`z*16+x`, biome stores in x,z,y order) was checked against a world BDS 1.26 generated.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name: str):
    mc_dir = str(DEPLOY / "minecraft")
    if mc_dir not in sys.path:
        sys.path.insert(0, mc_dir)
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"minecraft/{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


leveldb = _load("leveldb")
mapping = _load("mapping")


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _block(entries: list[tuple[bytes, bytes]]) -> bytes:
    body = b"".join(
        _varint(0) + _varint(len(k)) + _varint(len(v)) + k + v for k, v in entries
    )
    return body + struct.pack("<II", 0, 1)  # one restart point at offset 0


def write_table(path: Path, entries: list[tuple[bytes, int, bytes | None]]) -> None:
    """A one-data-block table, raw-deflate compressed like Mojang's LevelDB."""
    internal = sorted(
        (k + struct.pack("<Q", (seq << 8) | (1 if v is not None else 0)), v or b"")
        for k, seq, v in entries
    )
    data = _block(internal)
    deflate = zlib.compressobj(6, zlib.DEFLATED, -15)
    packed = deflate.compress(data) + deflate.flush()
    out = packed + bytes([4]) + b"\0\0\0\0"
    handle = _varint(0) + _varint(len(packed))
    index = _block([(internal[-1][0], handle)])
    index_off = len(out)
    out += index + bytes([0]) + b"\0\0\0\0"
    footer = _varint(0) + _varint(0) + _varint(index_off) + _varint(len(index))
    footer += b"\0" * (40 - len(footer)) + struct.pack("<Q", 0xDB4775248B80FB57)
    path.write_bytes(out + footer)


def write_log(path: Path, seq: int, ops: list[tuple[bytes, bytes | None]]) -> None:
    batch = struct.pack("<QI", seq, len(ops))
    for key, value in ops:
        if value is None:
            batch += b"\x00" + _varint(len(key)) + key
        else:
            batch += b"\x01" + _varint(len(key)) + key + _varint(len(value)) + value
    path.write_bytes(struct.pack("<IHB", 0, len(batch), 1) + batch)


def data3d(height_of, biome: int, top_biome: int | None = None) -> bytes:
    """A chunk record: heights from height_of(x, z), one biome everywhere, or a
    different one in the section holding the surface."""
    heights = struct.pack("<256h", *(height_of(i % 16, i // 16) for i in range(256)))
    stores = b""
    surface_section = (height_of(0, 0) - 1) // 16
    for section in range(24):
        b = top_biome if top_biome is not None and section == surface_section else biome
        stores += bytes([0]) + struct.pack("<i", b)  # bits 0: one value for the section
    return heights + stores


def test_the_reader_merges_tables_and_the_log_newest_first(tmp_path: Path) -> None:
    write_table(
        tmp_path / "000005.ldb",
        [(b"a", 1, b"old"), (b"b", 2, b"keep"), (b"c", 3, b"gone")],
    )
    write_log(
        tmp_path / "000006.log", 10, [(b"a", b"new"), (b"c", None), (b"d", b"logged")]
    )
    assert leveldb.read_db(tmp_path) == {b"a": b"new", b"b": b"keep", b"d": b"logged"}
    assert leveldb.read_db(tmp_path, wanted=lambda k: k == b"b") == {b"b": b"keep"}


def test_a_half_written_log_record_is_dropped_not_fatal(tmp_path: Path) -> None:
    write_log(tmp_path / "000006.log", 1, [(b"a", b"x" * 100)])
    raw = (tmp_path / "000006.log").read_bytes()
    (tmp_path / "000006.log").write_bytes(raw[:-30])
    assert leveldb.read_db(tmp_path) == {}


def test_heights_are_z_major_and_the_biome_is_read_at_the_surface() -> None:
    col = mapping.decode_data3d(data3d(lambda x, z: 70 + x, biome=1, top_biome=35))
    assert col.height[0 * 16 + 5] == 75  # z=0, x=5
    assert col.height[5 * 16 + 0] == 70  # z=5, x=0
    assert col.biome[0] == 35  # the section the surface sits in, not the one below


def test_a_tile_renders_generated_chunks_and_leaves_the_rest_clear(
    tmp_path: Path,
) -> None:
    flat = data3d(lambda x, z: 80, biome=1)
    ocean = data3d(lambda x, z: 60, biome=0)
    write_table(
        tmp_path / "000005.ldb",
        [
            (mapping.chunk_key(0, 0, 0, mapping.DATA3D), 1, flat),
            (mapping.chunk_key(1, 0, 0, mapping.DATA3D), 2, ocean),
            (
                mapping.chunk_key(0, 0, 1, mapping.DATA3D),
                3,
                flat,
            ),  # the Nether: not here
        ],
    )
    index = mapping.WorldIndex(tmp_path)
    index.refresh()
    assert index.extent(0) == {
        "min_x": 0,
        "max_x": 31,
        "min_z": 0,
        "max_z": 15,
        "chunks": 2,
    }
    assert index.extent(1)["chunks"] == 1
    image = mapping.tile(index, 0, 0, 0, 0)
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    rows = mapping.render_biome(index.columns(0, 0, 0, 16), 0, 0, 16)
    plains, sea, fog = (
        rows[8][8 * 4 : 8 * 4 + 4],
        rows[8][24 * 4 : 24 * 4 + 4],
        rows[8][40 * 4 : 40 * 4 + 4],
    )
    assert tuple(plains) == (*mapping.BIOME_COLORS[1], 255)  # flat: no hillshade
    assert sea[3] == 255 and sea[2] > sea[0]  # blue water
    assert fog[3] == 0  # never generated: transparent


def test_zoomed_out_tiles_sample_the_columns_they_show(tmp_path: Path) -> None:
    hill = data3d(lambda x, z: 70 + x + z, biome=1, top_biome=35)
    write_table(
        tmp_path / "000005.ldb", [(mapping.chunk_key(0, 0, 0, mapping.DATA3D), 1, hill)]
    )
    index = mapping.WorldIndex(tmp_path)
    index.refresh()
    col = mapping.decode_data3d(hill)
    for lx, lz in ((0, 0), (8, 8), (15, 3)):
        i = lz * 16 + lx
        assert mapping.surface_sample(hill, lx, lz) == (col.height[i], col.biome[i])
    # The furthest zoom out is one pixel per chunk: the chunk at (0, 0) is pixel (0, 0).
    overview = index.samples(0, 0, 0, 16 << mapping.MAX_ZOOM, 1 << mapping.MAX_ZOOM)
    assert list(overview) == [(8, 8)]
    rows = mapping.render_sampled(overview, 0, 0, mapping.TILE, 1 << mapping.MAX_ZOOM)
    assert rows[0][3] == 255 and rows[0][7] == 0 and rows[1][3] == 0
    assert mapping.tile(index, 0, mapping.MAX_ZOOM, 0, 0)[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(index.samples(0, 0, 0, 1, 2)) == 64  # zoom 1: every other column


def test_a_chunk_last_saved_before_1_18_is_drawn_not_left_as_a_hole(
    tmp_path: Path,
) -> None:
    def data2d(height: int, biome: int) -> bytes:
        return struct.pack("<256h", *([height] * 256)) + bytes([biome] * 256)

    write_table(
        tmp_path / "000005.ldb",
        [
            (
                mapping.chunk_key(0, 0, 0, mapping.DATA3D),
                1,
                data3d(lambda x, z: 144, 1),
            ),
            (mapping.chunk_key(1, 0, 0, mapping.DATA2D), 2, data2d(80, 4)),
            # Re-saved since the upgrade: both records exist, and Data3D is current.
            (mapping.chunk_key(2, 0, 0, mapping.DATA2D), 3, data2d(10, 0)),
            (
                mapping.chunk_key(2, 0, 0, mapping.DATA3D),
                4,
                data3d(lambda x, z: 144, 1),
            ),
        ],
    )
    index = mapping.WorldIndex(tmp_path)
    index.refresh()
    assert index.extent(0)["chunks"] == 3
    cols = index.columns(0, 0, 0, 3)
    # y=80 before 1.18 is y=80 after it: 144 above the new floor, like its neighbour.
    assert cols[(1, 0)].height[0] == 144 and cols[(1, 0)].biome[0] == 4
    assert cols[(2, 0)].biome[0] == 1
    assert index.samples(0, 0, 0, 3, 16)[(24, 8)] == (144, 4)
    rows = mapping.render_biome(cols, 0, 0, 16)
    assert tuple(rows[8][24 * 4 : 24 * 4 + 4]) == (*mapping.BIOME_COLORS[4], 255)


def _fake_predictor(tmp_path: Path, biome: int, height: int = 100) -> Path:
    """A stand-in for jbrain-predict: every sample is (height, biome); counts runs."""
    script = tmp_path / "jbrain-predict"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import struct, sys\n"
        f"open({str(tmp_path / 'runs')!r}, 'a').write('x')\n"
        "n = int(sys.argv[5])\n"
        f"sys.stdout.buffer.write(struct.pack('<hh', {height}, {biome}) * n * n)\n"
    )
    script.chmod(0o755)
    return script


def test_unvisited_ground_is_predicted_and_the_real_world_drawn_over_it(
    tmp_path: Path,
) -> None:
    db = tmp_path / "db"
    db.mkdir()
    write_table(
        db / "000005.ldb",
        [(mapping.chunk_key(0, 0, 0, mapping.DATA3D), 1, data3d(lambda x, z: 80, 1))],
    )
    index = mapping.WorldIndex(db)
    index.refresh()
    # Java's mangrove swamp (184) is Bedrock's 191: the prediction speaks Java's ids.
    predictor = mapping.Predictor(_fake_predictor(tmp_path, biome=184))
    grid = predictor.grid("123", 0, 0, 0, 2, 4)
    assert grid == {
        (0, 0): (100, 191),
        (4, 0): (100, 191),
        (0, 4): (100, 191),
        (4, 4): (100, 191),
    }
    predictor.grid("123", 0, 0, 0, 2, 4)
    assert (tmp_path / "runs").read_text() == "x"  # a seed always predicts the same
    assert predictor.grid("123", 1, 0, 0, 2, 4) == {}  # only the Overworld

    for zoom in (0, 2):
        image = mapping.tile(index, 0, zoom, 0, 0, predictor, "123")
        rows = _decode_png(image)
        assert tuple(rows[0][0:4]) == (*mapping.BIOME_COLORS[1], 255)  # the real chunk
        # Predicted ground is drawn, but as the survey: dimmer than explored ground.
        survey = rows[200][800:804]
        assert survey[3] == 255 and 0 < sum(survey[:3]) < sum(mapping.BIOME_COLORS[191])
    # No seed (a level.dat that doesn't say) or no binary: real chunks only.
    assert _decode_png(mapping.tile(index, 0, 0, 0, 0, predictor, None))[200][803] == 0
    broken = mapping.Predictor(tmp_path / "missing")
    assert _decode_png(mapping.tile(index, 0, 0, 0, 0, broken, "123"))[200][803] == 0


def _decode_png(data: bytes) -> list[bytes]:
    """The RGBA rows of a PNG the renderer wrote (filter 0, one IDAT)."""
    width = struct.unpack(">I", data[16:20])[0]
    pos, idat = 8, b""
    while pos < len(data):
        (size,) = struct.unpack(">I", data[pos : pos + 4])
        if data[pos + 4 : pos + 8] == b"IDAT":
            idat += data[pos + 8 : pos + 8 + size]
        pos += 12 + size
    raw = zlib.decompress(idat)
    line = width * 4 + 1
    return [raw[y * line + 1 : (y + 1) * line] for y in range(len(raw) // line)]


def test_the_index_rereads_on_change_but_not_more_often_than_a_minute(
    tmp_path: Path,
) -> None:
    write_table(
        tmp_path / "000005.ldb",
        [(mapping.chunk_key(0, 0, 0, mapping.DATA3D), 1, data3d(lambda x, z: 80, 1))],
    )
    now = [1000.0]
    index = mapping.WorldIndex(tmp_path, clock=lambda: now[0])
    index.refresh()
    write_log(
        tmp_path / "000006.log",
        5,
        [(mapping.chunk_key(3, 0, 0, mapping.DATA3D), data3d(lambda x, z: 90, 4))],
    )
    now[0] += 5
    index.refresh()  # a running server's log moves every few seconds: not yet
    assert index.extent(0)["chunks"] == 1
    now[0] += mapping.REREAD_S
    index.refresh()
    assert index.extent(0)["chunks"] == 2
    first = index._signature
    now[0] += mapping.REREAD_S
    index.refresh()
    assert index._signature is first  # nothing changed: no re-read


@pytest.mark.parametrize("bits", [1, 2, 4, 8])
def test_packed_biome_stores_decode_in_xzy_order(bits: int) -> None:
    per = 32 // bits
    words = -(-4096 // per)
    values = [(i >> 8) % 2 for i in range(4096)]  # biome alternates with x
    packed = []
    for w in range(words):
        word = 0
        for j in range(per):
            idx = w * per + j
            if idx < 4096:
                word |= values[idx] << (j * bits)
        packed.append(word)
    store = (
        bytes([bits << 1])
        + struct.pack(f"<{words}I", *packed)
        + struct.pack("<iii", 2, 7, 21)
    )
    decoded, end = mapping._palette_store(store, 0)
    assert end == len(store)
    assert decoded[0] == 7 and decoded[256] == 21  # x=0 → palette[0], x=1 → palette[1]
