"""Export the two signed-off builds as block lists the sidecar turns into .mcstructure.

    python3 export.py OUT_DIR [mojang-blocks.json]

Writes `pod.json.gz` and `mothership.json.gz`:

    {"size": [x, y, z], "palette": [{"name": "minecraft:…", "states": {…}}, …],
     "blocks": [[x, y, z, palette_index], …], "meta": {…}}

Coordinates are relative to the structure's minimum corner, in Minecraft axes. Render
stand-ins (`ladder_s`, `bed_head`, …) become real ids with their states. Lit redstone
lamps become ochre froglights, because an unpowered lamp goes dark on its first block
update. Fires are kept only where they can't spread: on netherrack (which burns
forever), with nothing flammable nearby and no TNT for a long way. Given Mojang's block
metadata (bedrock-samples `metadata/vanilladata_modules/mojang-blocks.json`), every
palette entry is checked against it, because a wrong state set breaks a block silently.
"""

from __future__ import annotations

import gzip
import io
import json
import runpy
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

FLAMMABLE = {
    "coal_block", "oak_planks", "barrel", "bookshelf", "tnt", "hay_block", "oak_leaves",
    "oak_log", "crafting_table", "white_wool", "orange_wool", "white_carpet",
    "orange_carpet", "red_carpet", "bed_head", "bed_feet", "smithing_table",
}
# facing_direction: 2 north, 3 south, 4 west, 5 east. A ladder faces away from its wall.
LADDER_FACING = {"s": 2, "n": 3, "e": 4, "w": 5}
# bed direction: the way from the foot to the head — 0 south, 1 west, 2 north, 3 east.
BED_DIRECTION = {(0, 1): 0, (-1, 0): 1, (0, -1): 2, (1, 0): 3}
RENAME = {"redstone_lamp": "ochre_froglight", "iron_trapdoor_flat": "iron_trapdoor"}


def bedrock(name: str, pos: tuple[int, int, int], blocks: dict) -> tuple[str, dict]:
    x, y, z = pos
    if name.startswith("ladder_"):
        return "minecraft:ladder", {"facing_direction": LADDER_FACING[name[-1]]}
    if name in ("bed_head", "bed_feet"):
        other = "bed_feet" if name == "bed_head" else "bed_head"
        for (dx, dz), d in BED_DIRECTION.items():
            if blocks.get((x + dx, y, z + dz)) == other:
                # From this half to its partner; flip when this is the head.
                direction = d if name == "bed_feet" else BED_DIRECTION[(-dx, -dz)]
                return "minecraft:bed", {
                    "direction": direction,
                    "head_piece_bit": name == "bed_head",
                    "occupied_bit": False,
                }
        raise ValueError(f"a {name} at {pos} has no other half")
    original = name
    name = RENAME.get(name, name)
    states: dict[str, Any] = {}
    if name in ("polished_basalt", "ochre_froglight", "oak_log", "hay_block"):
        # Lie along a run of the same block (a turret barrel, a console strip), unless it
        # also has one above or below; neighbours are looked up by the design's name.
        axis = "y"
        for a, (dx, dz) in (("x", (1, 0)), ("z", (0, 1))):
            if blocks.get((x + dx, y, z + dz)) == original or blocks.get((x - dx, y, z - dz)) == original:
                if blocks.get((x, y + 1, z)) != original and blocks.get((x, y - 1, z)) != original:
                    axis = a
        states["pillar_axis"] = axis
        if name == "hay_block":
            states["deprecated"] = 0
    elif name == "campfire":
        states = {"extinguished": False, "minecraft:cardinal_direction": "south"}
    elif name in ("furnace", "anvil"):
        states = {"minecraft:cardinal_direction": "north"}
    elif name == "barrel":
        states = {"facing_direction": 1, "open_bit": False}
    elif name == "oak_leaves":
        states = {"persistent_bit": True, "update_bit": False}
    elif name == "farmland":
        states = {"moisturized_amount": 7}
    elif name in ("wheat", "carrots"):
        states = {"growth": 7}
    elif name == "lantern":
        states = {"hanging": False}
    elif name == "iron_trapdoor":
        states = {"direction": 0, "open_bit": False, "upside_down_bit": False}
    elif name == "tnt":
        states = {"explode_bit": False}
    elif name == "fire":
        states = {"age": 0}
    return f"minecraft:{name}", states


def safe_fires(blocks: dict[tuple[int, int, int], str]) -> int:
    """Drop fires that could spread; set the rest on netherrack so they never go out."""
    tnt = [p for p, b in blocks.items() if b == "tnt"]
    dropped = 0
    for p in [p for p, b in blocks.items() if b == "fire"]:
        x, y, z = p
        near = any(
            blocks.get((x + dx, y + dy, z + dz)) in FLAMMABLE
            for dx in range(-2, 3)
            for dz in range(-2, 3)
            # Fire lights air at ±1 and -1..+4, which catches from its own neighbours.
            for dy in range(-2, 6)
            if (dx, dy, dz) != (0, -1, 0)
        )
        below = blocks.get((x, y - 1, z))
        far_from_tnt = all(max(abs(x - t[0]), abs(y - t[1]), abs(z - t[2])) > 12 for t in tnt)
        if near or below is None or below in FLAMMABLE - {"coal_block"} or not far_from_tnt:
            del blocks[p]
            dropped += 1
        else:
            blocks[(x, y - 1, z)] = "netherrack"
    return dropped


def pack(blocks: dict[tuple[int, int, int], str], air: set, meta: dict) -> dict:
    cells = dict(blocks)
    for p in air:
        cells.setdefault(p, "air")
    xs = [p[0] for p in cells]
    ys = [p[1] for p in cells]
    zs = [p[2] for p in cells]
    lo = (min(xs), min(ys), min(zs))
    palette: list[dict] = []
    index: dict[str, int] = {}
    out = []
    for p, b in sorted(cells.items()):
        name, states = ("minecraft:air", {}) if b == "air" else bedrock(b, p, blocks)
        key = json.dumps([name, states], sort_keys=True)
        if key not in index:
            index[key] = len(palette)
            palette.append({"name": name, "states": states})
        out.append([p[0] - lo[0], p[1] - lo[1], p[2] - lo[2], index[key]])
    size = [max(xs) - lo[0] + 1, max(ys) - lo[1] + 1, max(zs) - lo[2] + 1]
    return {"size": size, "palette": palette, "blocks": out, "meta": {**meta, "origin": list(lo)}}


def check(palette: list[dict], mojang: Path) -> None:
    data = json.loads(mojang.read_text())
    props = {p["name"]: {v["value"] for v in p["values"]} for p in data["block_properties"]}
    kinds = {p["name"]: p["type"] for p in data["block_properties"]}
    py_type = {"bool": bool, "int": int, "string": str}
    known = {b["name"]: [p["name"] for p in b.get("properties", [])] for b in data["data_items"]}
    for entry in palette:
        name, states = entry["name"], entry["states"]
        if name not in known:
            raise ValueError(f"{name} is not a block in this game version")
        if sorted(states) != sorted(known[name]):
            raise ValueError(f"{name}: states {sorted(states)} but the game has {sorted(known[name])}")
        for key, value in states.items():
            # True == 1 in Python: the type decides byte vs int in the file, so check it.
            if type(value) is not py_type[kinds[key]]:
                raise ValueError(f"{name}: {key}={value!r} should be a {kinds[key]}")
            if value not in props[key]:
                raise ValueError(f"{name}: {key}={value!r} is not one of {sorted(props[key], key=str)}")


def run(script: str, *args: str) -> dict[str, Any]:
    old = sys.argv
    sys.argv = [script, *args]
    try:
        with redirect_stdout(io.StringIO()):
            return runpy.run_path(str(HERE / script), run_name="__main__")
    finally:
        sys.argv = old


def main() -> None:
    out = Path(sys.argv[1])
    mojang = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="wreck-export-"))  # the generators' scene files

    pod = run("gen_pod.py", str(tmp / "pod.json"))
    pod_blocks = {p: b for p, b in pod["ship"].items()}
    fires = safe_fires(pod_blocks)
    shield = pod["R"] @ [-0.5, 0, 0] + pod["offset"]
    nose = pod["R"] @ [11.5, 0, 0] + pod["offset"]
    pod_meta = {
        "deck_y": int(pod["FLOOR"]),
        "hatch_out": list(pod["hatch_out"]) if pod["hatch_out"] else None,
        "hatch_dir": [int(pod["hx"] * pod["sgn"]), int(pod["hz"] * pod["sgn"])],
        "axis_xz": [round(float(v), 4) for v in pod["axis_xz"]],
        "shield": [round(float(v), 2) for v in shield],
        "nose": [round(float(v), 2) for v in nose],
        "barrels": sorted([list(p) for p, b in pod_blocks.items() if b == "barrel"]),
        "fires_dropped": fires,
    }
    pod_out = pack(pod_blocks, set(pod["interior"]), pod_meta)

    ship = run("gen_battleship.py", str(tmp / "pod.json"), str(tmp / "scene.json"))
    ship_blocks = dict(ship["blocks"])
    fires = safe_fires(ship_blocks)
    ship_meta = {
        # Where the ship sat over the pod in the signed-off scene. Block coordinates are
        # relative to the build's corner, so world = local + origin + offset.
        "offset": [int(ship["SX"]), int(ship["ALT"]), int(ship["SZ"])],
        "barrels": sorted([list(p) for p, b in ship_blocks.items() if b == "barrel"]),
        "fires_dropped": fires,
    }
    ship_out = pack(ship_blocks, set(), ship_meta)

    for name, data in (("pod", pod_out), ("mothership", ship_out)):
        if mojang:
            check(data["palette"], mojang)
        # Rebase metadata positions given in world coords onto the structure's corner.
        lo = data["meta"]["origin"]
        for key in ("hatch_out", "shield", "nose"):
            if data["meta"].get(key):
                data["meta"][key] = [round(v - o, 2) for v, o in zip(data["meta"][key], lo)]
        for key in ("barrels",):
            data["meta"][key] = [[x - lo[0], y - lo[1], z - lo[2]] for x, y, z in data["meta"][key]]
        raw = json.dumps(data, separators=(",", ":")).encode()
        (out / f"{name}.json.gz").write_bytes(gzip.compress(raw, mtime=0))
        print(name, "size", data["size"], "blocks", len(data["blocks"]),
              "palette", len(data["palette"]), "fires dropped", data["meta"]["fires_dropped"])


if __name__ == "__main__":
    main()
