"""The spawn wreck's builds as Bedrock `.mcstructure` files (plan §SW1).

The escape pod and the mothership were designed in `spikes/minecraft-spawn-wreck/`,
whose `export.py` writes them here as block lists (`wreck/*.json.gz`). This turns them,
at image build, into the structure files and the data module the `jbrain-wreck` pack
places from:

    python3 structures.py build wreck wreck-pack

A `.mcstructure` is one uncompressed little-endian NBT compound. Its blocks are palette
indexes in x-major, then y, then z order, with -1 meaning "leave what's there". So the
pod's interior is explicit air, which clears any terrain inside the hull, while the
ship's surroundings are -1 and never overwrite a hill. The mothership is cut into
64 × 64 columns: Bedrock loads larger files, but no tool of Mojang's makes them, and
64 keeps each placement a bounded amount of work.
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

# The block-state version stamped on each palette entry (1.21.60.33); a newer game
# upgrades older entries on load, so one fixed stamp serves every server version.
BLOCK_VERSION = 18168865
TILE = 64
NAMESPACE = "jbrain"
# Beds keep their colour in a block entity; without one the game draws them blank.
BED_COLOR_RED = 14

_END, _BYTE, _INT, _STRING, _LIST, _COMPOUND = 0, 1, 3, 8, 9, 10


class Byte(int):
    """An NBT byte, so a bool-ish state isn't written as an int."""


# Mapping/Sequence (covariant), so nested literals type-check as NBT.
Nbt = bool | int | str | Mapping[str, "Nbt"] | Sequence["Nbt"]


def _tag(value: Nbt) -> int:
    if isinstance(value, bool | Byte):
        return _BYTE
    if isinstance(value, int):
        return _INT
    if isinstance(value, str):
        return _STRING
    if isinstance(value, Sequence):
        return _LIST
    return _COMPOUND


def _string(out: bytearray, text: str) -> None:
    raw = text.encode()
    out += struct.pack("<H", len(raw)) + raw


def _payload(out: bytearray, value: Nbt) -> None:
    if isinstance(value, bool | Byte):
        out += struct.pack("<b", int(value))
    elif isinstance(value, int):
        out += struct.pack("<i", value)
    elif isinstance(value, str):
        _string(out, value)
    elif isinstance(value, Sequence):
        inner = _tag(value[0]) if value else _COMPOUND
        if any(_tag(v) != inner for v in value):
            raise ValueError("an NBT list holds one type")
        out += struct.pack("<bi", inner, len(value))
        if inner == _INT:
            out += struct.pack(f"<{len(value)}i", *value)
        else:
            for item in value:
                _payload(out, item)
    else:
        for key, item in value.items():
            out += struct.pack("<b", _tag(item))
            _string(out, key)
            _payload(out, item)
        out += struct.pack("<b", _END)


def nbt(root: Mapping[str, Nbt]) -> bytes:
    out = bytearray(struct.pack("<b", _COMPOUND))
    _string(out, "")
    _payload(out, root)
    return bytes(out)


def read_nbt(data: bytes) -> dict[str, Any]:
    """The inverse of `nbt`, for the tests and for checking a built file."""
    pos = 0

    def take(fmt: str) -> Any:
        nonlocal pos
        size = struct.calcsize(fmt)
        vals = struct.unpack_from(fmt, data, pos)
        pos += size
        return vals[0] if len(vals) == 1 else vals

    def text() -> str:
        nonlocal pos
        n = take("<H")
        pos += n
        return data[pos - n : pos].decode()

    def payload(tag: int) -> Any:
        nonlocal pos
        if tag == _BYTE:
            return take("<b")
        if tag == _INT:
            return take("<i")
        if tag == _STRING:
            return text()
        if tag == _LIST:
            inner, count = take("<b"), take("<i")
            if inner == _INT:
                vals = struct.unpack_from(f"<{count}i", data, pos)
                pos += 4 * count
                return list(vals)
            return [payload(inner) for _ in range(count)]
        if tag == _COMPOUND:
            out: dict[str, Any] = {}
            while (inner := take("<b")) != _END:
                key = text()  # the name precedes the value in the stream
                out[key] = payload(inner)
            return out
        raise ValueError(f"unexpected NBT tag {tag}")

    if take("<b") != _COMPOUND:
        raise ValueError("not an NBT compound")
    text()
    return payload(_COMPOUND)


def _states(states: dict[str, Any]) -> dict[str, Nbt]:
    # JSON has no byte type: a bool state is a byte, an int one an int.
    return {k: Byte(v) if isinstance(v, bool) else v for k, v in states.items()}


def mcstructure(
    size: list[int], palette: list[dict[str, Any]], blocks: list[list[int]]
) -> bytes:
    """One structure file: `blocks` are [x, y, z, palette_index] inside `size`."""
    sx, sy, sz = size
    indices = [-1] * (sx * sy * sz)
    used: dict[int, int] = {}
    entities: dict[str, Nbt] = {}
    local: list[dict[str, Nbt]] = []
    for x, y, z, i in blocks:
        if not (0 <= x < sx and 0 <= y < sy and 0 <= z < sz):
            # An index past a row's end would land the block somewhere else entirely.
            raise ValueError(f"block ({x}, {y}, {z}) is outside a {size} structure")
        if i not in used:
            used[i] = len(local)
            entry = palette[i]
            local.append(
                {
                    "name": entry["name"],
                    "states": _states(entry["states"]),
                    "version": BLOCK_VERSION,
                }
            )
        at = (x * sy + y) * sz + z
        indices[at] = used[i]
        if palette[i]["name"] == "minecraft:bed":
            entities[str(at)] = {
                "block_entity_data": {
                    "id": "Bed",
                    "color": Byte(BED_COLOR_RED),
                    "isMovable": Byte(1),
                    "x": x,
                    "y": y,
                    "z": z,
                }
            }
    root: dict[str, Nbt] = {
        "format_version": 1,
        "size": [sx, sy, sz],
        "structure": {
            "block_indices": [indices, [-1] * len(indices)],
            "entities": [],
            "palette": {
                "default": {"block_palette": local, "block_position_data": entities}
            },
        },
        "structure_world_origin": [0, 0, 0],
    }
    return nbt(root)


def tiles(
    size: list[int], blocks: list[list[int]], tile: int = TILE
) -> list[dict[str, Any]]:
    """Cut a build into tile × full-height × tile columns, each with its own origin."""
    sx, sy, sz = size
    groups: dict[tuple[int, int], list[list[int]]] = {}
    for x, y, z, i in blocks:
        groups.setdefault((x // tile, z // tile), []).append(
            [x % tile, y, z % tile, i]
        )
    out = []
    for (tx, tz), cells in sorted(groups.items()):
        out.append(
            {
                "x": tx * tile,
                "z": tz * tile,
                "size": [min(tile, sx - tx * tile), sy, min(tile, sz - tz * tile)],
                "blocks": cells,
            }
        )
    return out


def load(path: Path) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()))


def build(src: Path, pack: Path) -> dict[str, Any]:
    """Write every structure into `pack`/structures/jbrain/ and the facts the pack's
    script needs (sizes, tile origins, anchor points) into `pack`/scripts/data.js."""
    out_dir = pack / "structures" / NAMESPACE
    out_dir.mkdir(parents=True, exist_ok=True)
    pod = load(src / "pod.json.gz")
    (out_dir / "pod.mcstructure").write_bytes(
        mcstructure(pod["size"], pod["palette"], pod["blocks"])
    )
    ship = load(src / "mothership.json.gz")
    ship_tiles = []
    for n, t in enumerate(tiles(ship["size"], ship["blocks"])):
        name = f"ship_{n}"
        (out_dir / f"{name}.mcstructure").write_bytes(
            mcstructure(t["size"], ship["palette"], t["blocks"])
        )
        ship_tiles.append(
            {"id": f"{NAMESPACE}:{name}", "x": t["x"], "z": t["z"], "size": t["size"]}
        )
    data = {
        "pod": {"id": f"{NAMESPACE}:pod", "size": pod["size"], **pod["meta"]},
        "ship": {"size": ship["size"], "tiles": ship_tiles, **ship["meta"]},
    }
    scripts = pack / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "data.js").write_text(
        "// Generated by deploy/minecraft/structures.py at image build; do not edit.\n"
        f"export const WRECK = {json.dumps(data, indent=1)};\n"
    )
    return data


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] != "build":
        sys.exit("usage: structures.py build SRC_DIR PACK_DIR")
    data = build(Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"[structures] pod + {len(data['ship']['tiles'])} ship tiles", flush=True)


if __name__ == "__main__":
    main()
