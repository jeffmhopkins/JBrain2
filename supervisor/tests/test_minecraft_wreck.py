"""The spawn wreck's first wave (plan §SW1): the builds become valid structure files,
and only a world the box generates itself is armed to receive them.

What's pinned is the part a wrong guess would cost most: the structure file's byte
layout (a mis-ordered index scrambles the whole build), the fire rules that keep the
mothership from burning (or blowing up its magazine), and arming, which must never
reach the owner's imported or restored worlds.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
MC = DEPLOY / "minecraft"


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    if str(MC) not in sys.path:
        sys.path.insert(0, str(MC))
    spec = importlib.util.spec_from_file_location(name, MC / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


install = _load("install", "install.py")
worlds = _load("worlds", "worlds.py")
bds = _load("bds", "bds.py")
structures = _load("structures", "structures.py")
server = _load("mc_server", "server.py")

WRECK_PACK_ID = json.loads((MC / "wreck-pack/manifest.json").read_text())["header"][
    "uuid"
]


@pytest.fixture(autouse=True)
def _data_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data = tmp_path / "data"
    monkeypatch.setattr(server, "DATA", data)
    monkeypatch.setattr(server, "SERVER_DIR", data / "server")
    monkeypatch.setattr(server, "SNAPSHOT_DIR", data / "snapshots")
    monkeypatch.setattr(server, "OVERRIDES", data / "properties.json")
    monkeypatch.setattr(server, "SETTINGS", data / "settings.json")
    monkeypatch.setattr(server, "SLOTS", data / "slots.json")
    monkeypatch.setattr(server, "SNAPSHOT_INDEX", data / "snapshot-index.json")
    monkeypatch.setattr(server, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(server.install, "apply_properties", lambda p, o: None)


# ---------------------------------------------------------------- the structure file


def test_nbt_round_trips_every_tag_the_structure_uses() -> None:
    root = {
        "i": 7,
        "b": structures.Byte(1),
        "t": True,
        "s": "minecraft:ladder",
        "ints": [-1, 0, 5],
        "nested": [[1, 2], [3]],
        "c": {"inner": {"x": 1}},
        "empty": [],
    }
    back = structures.read_nbt(structures.nbt(root))
    assert back == {**root, "b": 1, "t": 1}


def test_blocks_are_indexed_x_major_then_y_then_z_and_the_rest_is_left_alone() -> None:
    palette = [
        {"name": "minecraft:stone", "states": {}},
        {"name": "minecraft:ladder", "states": {"facing_direction": 2}},
        {"name": "minecraft:unused", "states": {}},
    ]
    blob = structures.mcstructure(
        [2, 3, 4], palette, [[1, 2, 3, 1], [0, 0, 1, 0], [1, 0, 0, 0]]
    )
    d = structures.read_nbt(blob)
    assert d["format_version"] == 1 and d["size"] == [2, 3, 4]
    layer, extra = d["structure"]["block_indices"]
    assert len(layer) == len(extra) == 24 and set(extra) == {-1}
    local = d["structure"]["palette"]["default"]["block_palette"]
    assert [p["name"] for p in local] == ["minecraft:ladder", "minecraft:stone"]
    assert local[0]["states"] == {"facing_direction": 2}
    assert local[0]["version"] == structures.BLOCK_VERSION

    def at(x: int, y: int, z: int) -> int:
        return layer[(x * 3 + y) * 4 + z]

    assert (at(1, 2, 3), at(0, 0, 1), at(1, 0, 0)) == (0, 1, 1)
    assert sum(1 for v in layer if v == -1) == 21  # untouched cells keep the terrain


def test_a_bed_carries_its_colour_and_a_bool_state_is_a_byte() -> None:
    palette = [
        {
            "name": "minecraft:bed",
            "states": {"direction": 0, "head_piece_bit": True, "occupied_bit": False},
        }
    ]
    d = structures.read_nbt(structures.mcstructure([1, 1, 2], palette, [[0, 0, 1, 0]]))
    entry = d["structure"]["palette"]["default"]["block_palette"][0]
    assert entry["states"] == {"direction": 0, "head_piece_bit": 1, "occupied_bit": 0}
    data = d["structure"]["palette"]["default"]["block_position_data"]
    assert data["1"]["block_entity_data"]["id"] == "Bed"
    assert data["1"]["block_entity_data"]["color"] == structures.BED_COLOR_RED


def test_tiles_cover_every_block_once_with_their_own_origins() -> None:
    blocks = [[x, 0, z, 0] for x in range(0, 140, 7) for z in range(0, 70, 5)]
    cut = structures.tiles([140, 1, 70], blocks)
    assert [(t["x"], t["z"]) for t in cut] == [
        (0, 0),
        (0, 64),
        (64, 0),
        (64, 64),
        (128, 0),
        (128, 64),
    ]
    assert cut[-1]["size"] == [12, 1, 6]
    back = sorted(
        [t["x"] + x, y, t["z"] + z, i] for t in cut for x, y, z, i in t["blocks"]
    )
    assert back == sorted(blocks)
    assert all(
        0 <= x < t["size"][0] and 0 <= z < t["size"][2]
        for t in cut
        for x, _y, z, _i in t["blocks"]
    )


# ---------------------------------------------------------------- the committed builds


def _data(name: str) -> dict[str, Any]:
    return json.loads(gzip.decompress((MC / "wreck" / f"{name}.json.gz").read_bytes()))


def _cells(d: dict[str, Any]) -> dict[tuple[int, int, int], str]:
    names = [p["name"].removeprefix("minecraft:") for p in d["palette"]]
    return {(x, y, z): names[i] for x, y, z, i in d["blocks"]}


def test_the_pod_clears_its_inside_and_keeps_its_hatch_on_the_deck() -> None:
    pod = _data("pod")
    cells = _cells(pod)
    sx, sy, sz = pod["size"]
    assert all(0 <= x < sx and 0 <= y < sy and 0 <= z < sz for x, y, z in cells)
    assert "air" in cells.values()  # terrain inside the hull is cleared on placement
    hx, hy, hz = pod["meta"]["hatch_out"]
    assert hy == pod["meta"]["deck_y"] - pod["meta"]["origin"][1]
    # A player-sized way out: outside the hull or cleared, never a block.
    assert {cells.get((hx, hy, hz)), cells.get((hx, hy + 1, hz))} <= {None, "air"}
    assert len(pod["meta"]["barrels"]) >= 1
    assert all(cells[tuple(b)] == "barrel" for b in pod["meta"]["barrels"])


def test_every_bed_has_its_other_half() -> None:
    for name in ("pod", "mothership"):
        d = _data(name)
        heads = []
        for x, y, z, i in d["blocks"]:
            p = d["palette"][i]
            if p["name"] == "minecraft:bed" and p["states"]["head_piece_bit"]:
                heads.append((x, y, z, p["states"]["direction"]))
        feet = {
            (x, y, z)
            for x, y, z, i in d["blocks"]
            if d["palette"][i]["name"] == "minecraft:bed"
            and not d["palette"][i]["states"]["head_piece_bit"]
        }
        step = {0: (0, 1), 1: (-1, 0), 2: (0, -1), 3: (1, 0)}
        assert heads
        for x, y, z, direction in heads:
            dx, dz = step[direction]
            assert (x - dx, y, z - dz) in feet  # the foot is behind the head


def test_the_mothership_cannot_burn_or_blow_up() -> None:
    """Fire spreads to flammable blocks within a few blocks (further up), and TNT goes
    off from it: every fire kept sits on netherrack with neither anywhere near."""
    cells = _cells(_data("mothership"))
    flammable = {
        "coal_block",
        "oak_planks",
        "barrel",
        "bookshelf",
        "tnt",
        "hay_block",
        "oak_leaves",
        "crafting_table",
        "bed",
    }
    tnt = [p for p, b in cells.items() if b == "tnt"]
    fires = [p for p, b in cells.items() if b == "fire"]
    assert tnt and fires
    for x, y, z in fires:
        assert cells[(x, y - 1, z)] == "netherrack"
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                for dy in range(0, 5):
                    assert cells.get((x + dx, y + dy, z + dz)) not in flammable
        assert all(max(abs(x - a), abs(y - b), abs(z - c)) > 12 for a, b, c in tnt)


def test_build_writes_the_pod_the_ship_tiles_and_the_scripts_data(
    tmp_path: Path,
) -> None:
    data = structures.build(MC / "wreck", tmp_path)
    out = tmp_path / "structures" / "jbrain"
    assert (out / "pod.mcstructure").is_file()
    ship = _data("mothership")
    placed = 0
    for t in data["ship"]["tiles"]:
        name = t["id"].split(":")[1]
        d = structures.read_nbt((out / f"{name}.mcstructure").read_bytes())
        assert d["size"] == t["size"]
        placed += sum(1 for v in d["structure"]["block_indices"][0] if v != -1)
    assert placed == len(ship["blocks"])
    js = (tmp_path / "scripts" / "data.js").read_text()
    assert js.startswith("// Generated") and '"jbrain:pod"' in js
    parsed = json.loads(js.split("=", 1)[1].rstrip().rstrip(";"))
    assert parsed["pod"]["hatch_out"] == _data("pod")["meta"]["hatch_out"]


# ---------------------------------------------------------------- arming


class _FakeBds:
    def __init__(self) -> None:
        self.running = False
        self.players: dict[str, Any] = {}
        self.world = "world"
        self.replaced: list[str] = []

    def note_world_replaced(self, folder: str) -> None:
        self.replaced.append(folder)

    def stop(self) -> None:
        self.running = False

    def start(self) -> None:
        self.running = True


def _rig() -> Any:
    rig = server.Rig(env={})
    rig.bds = _FakeBds()
    return rig


def _make_world(folder: str) -> Path:
    world = server.SERVER_DIR / "worlds" / folder
    (world / "db").mkdir(parents=True)
    (world / "level.dat").write_bytes(b"lvl")
    return world


def test_a_world_the_box_creates_is_armed_unless_the_owner_says_no() -> None:
    rig = _rig()
    assert rig.settings()["spawn_wreck"] is True  # on by default
    assert rig.create_slot("slot2", {"name": "A"})["wreck"] == "armed"
    assert rig.create_slot("slot3", {"name": "B", "wreck": False})["wreck"] is None
    rig.set_settings({"spawn_wreck": False})
    assert rig.create_slot("slot4", {"name": "C"})["wreck"] is None
    assert rig.create_slot("slot5", {"name": "D", "wreck": True})["wreck"] == "armed"
    with pytest.raises(ValueError):
        rig.slots.update("slot2", wreck="maybe")


def test_a_reset_rearms_but_an_import_or_restore_never_does(tmp_path: Path) -> None:
    _make_world("world")
    _make_world("slot2")
    rig = _rig()
    rig.slots.update("slot2", seed="777", wreck="done")
    assert rig.reset_slot("slot2", "same_seed")["wreck"] == "armed"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("level.dat", b"x")
        zf.writestr("db/1.ldb", b"y")
    upload = tmp_path / "w.mcworld"
    upload.write_bytes(buf.getvalue())
    rig.slots.update("slot3", wreck="armed")
    assert rig.import_slot("slot3", upload)["wreck"] is None

    rig.slots.update("slot1", wreck="armed")
    snap = rig.snapshot("manual", folder="world")
    assert rig.restore_snapshot(snap["name"], "slot1")["wreck"] is None


def test_the_pack_reaches_an_armed_world_before_bds_generates_it() -> None:
    rig = _rig()
    rig.create_slot("slot2", {"name": "New"})
    rig._apply_slot(rig.slots.get("slot2"))
    world = server.SERVER_DIR / "worlds" / "slot2"
    assert not world.exists()
    rig._install_wreck()
    listed = json.loads((world / "world_behavior_packs.json").read_text())
    assert [p["pack_id"] for p in listed] == [WRECK_PACK_ID]
    assert (world / "behavior_packs" / "jbrain_wreck" / "scripts" / "main.js").exists()
    assert not rig.slots.get("slot2")["exists"]  # still BDS's to generate


def test_the_pack_comes_out_of_a_world_that_is_not_armed() -> None:
    world = _make_world("world")
    rig = _rig()
    rig.slots.update("slot1", wreck="armed")
    rig._install_wreck()
    assert (world / "behavior_packs" / "jbrain_wreck").is_dir()
    rig.slots.update("slot1", wreck="done")
    rig._install_wreck()
    assert not (world / "behavior_packs" / "jbrain_wreck").exists()
    assert json.loads((world / "world_behavior_packs.json").read_text()) == []
    rig.slots.update("slot2", wreck=None)
    rig._apply_slot(rig.slots.get("slot2"))
    rig._install_wreck()
    assert not (server.SERVER_DIR / "worlds" / "slot2").exists()  # nothing to make


def test_the_wrecks_done_event_marks_only_its_own_armed_world(tmp_path: Path) -> None:
    rig = _rig()
    real = bds.Bds(tmp_path)
    rig.bds = real
    rig.slots.update("slot2", wreck="armed")
    rig.slots.update("slot3", wreck=None)
    real.world = "slot2"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"loaded"}')
    rig.note_wreck()
    assert rig.slots.get("slot2")["wreck"] == "armed"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"done"}')
    real.world = "slot3"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"done"}')
    rig.note_wreck()
    assert rig.slots.get("slot2")["wreck"] == "done"
    assert rig.slots.get("slot3")["wreck"] is None  # never armed: stays that way
    (ev,) = [e for e in real.events_after(0) if e.get("phase") == "loaded"]
    assert ev["kind"] == "wreck" and ev["world"] == "slot2"


def test_the_wreck_pack_is_its_own_pack_on_stable_scripting() -> None:
    manifest = json.loads((MC / "wreck-pack/manifest.json").read_text())
    jbrain = json.loads((MC / "jbrain-pack/manifest.json").read_text())
    assert manifest["header"]["uuid"] != jbrain["header"]["uuid"]
    deps = {d["module_name"]: d["version"] for d in manifest["dependencies"]}
    assert deps == {"@minecraft/server": "2.0.0"}  # stable, no experiments
