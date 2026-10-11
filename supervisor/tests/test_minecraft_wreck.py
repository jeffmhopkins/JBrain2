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
import shutil
import subprocess
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
    """Fire lights air beside it (±1, from one below to four above), and that air
    catches from its own neighbours; TNT goes off from it. Every fire kept sits on
    netherrack with nothing flammable in that reach and no TNT anywhere near."""
    cells = _cells(_data("mothership"))
    flammable = {
        "coal_block", "oak_planks", "barrel", "bookshelf", "tnt", "hay_block",
        "oak_leaves", "oak_log", "crafting_table", "smithing_table", "bed",
        "white_wool", "orange_wool", "white_carpet", "orange_carpet", "red_carpet",
    }  # fmt: skip
    assert "red_carpet" in cells.values()  # the set covers what the ship really holds
    tnt = [p for p, b in cells.items() if b == "tnt"]
    fires = [p for p, b in cells.items() if b == "fire"]
    assert tnt and fires
    for x, y, z in fires:
        assert cells[(x, y - 1, z)] == "netherrack"
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                for dy in range(-2, 6):
                    if (dx, dy, dz) != (0, -1, 0):
                        near = cells.get((x + dx, y + dy, z + dz))
                        assert near not in flammable
        assert all(max(abs(x - a), abs(y - b), abs(z - c)) > 12 for a, b, c in tnt)


def test_the_ship_hangs_where_it_was_signed_off_over_the_pod() -> None:
    """Block coordinates are relative to each build's corner, so a build's world corner
    is origin + offset: the ship's sits ~100 above the pod's, as in the renders."""
    pod, ship = _data("pod")["meta"], _data("mothership")["meta"]
    corner = [o + d for o, d in zip(ship["origin"], ship["offset"], strict=True)]
    assert corner == [-106, 164, -95]
    assert corner[1] - pod["origin"][1] == 104


def test_lamps_in_a_row_lie_along_it() -> None:
    d = _data("mothership")
    axes = {
        p["states"]["pillar_axis"]
        for p in d["palette"]
        if p["name"] == "minecraft:ochre_froglight"
    }
    assert axes > {"y"}  # console strips are horizontal, not all upright


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

    def events_after(self, _after: int) -> list[dict[str, Any]]:
        return []

    def wait_running(self, _timeout_s: float) -> bool:
        return False  # the first-start watch has nothing to watch here

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


def test_a_reset_takes_the_owners_choice_over_the_default() -> None:
    _make_world("slot2")
    rig = _rig()
    rig.slots.update("slot2", seed="777", wreck="done")
    assert rig.reset_slot("slot2", "same_seed", wreck=False)["wreck"] is None
    rig.set_settings({"spawn_wreck": False})
    assert rig.reset_slot("slot2", "same_seed")["wreck"] is None
    assert rig.reset_slot("slot2", "new_seed", wreck=True)["wreck"] == "armed"
    body = {"mode": "same_seed", "wreck": False}
    assert server.Handler._world_action(rig, "slot2", "reset", body)["wreck"] is None


def test_a_test_world_keeps_its_mark_only_until_the_owner_takes_it_over(
    tmp_path: Path,
) -> None:
    rig = _rig()
    assert rig.create_slot("slot2", {"name": "T", "test": True})["test"] is True
    assert rig.create_slot("slot3", {"name": "Mine"})["test"] is False
    _make_world("slot2")
    rig.slots.update("slot2", seed="7")
    body = {"mode": "same_seed", "test": True}
    assert server.Handler._world_action(rig, "slot2", "reset", body)["test"] is True
    assert rig.reset_slot("slot2", "same_seed")["test"] is False  # the owner's now

    rig.slots.update("slot2", test=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("level.dat", b"x")
        zf.writestr("db/1.ldb", b"y")
    upload = tmp_path / "w.mcworld"
    upload.write_bytes(buf.getvalue())
    assert rig.import_slot("slot2", upload)["test"] is False

    rig.slots.update("slot2", test=True)
    snap = rig.snapshot("manual", folder="slot2")
    assert rig.restore_snapshot(snap["name"], "slot2")["test"] is False


def test_the_sidecar_never_makes_an_owner_world_a_test_one() -> None:
    """The debug API checks first; the sidecar refuses too, so a race or a bad check
    still can't put the owner's world in the token's reach."""
    _make_world("slot3")
    rig = _rig()
    rig.create_slot("slot2", {"name": "Not generated yet"})
    with pytest.raises(ValueError):
        rig.create_slot("slot2", {"name": "T", "test": True})
    assert rig.slots.get("slot2")["name"] == "Not generated yet"
    rig.slots.update("slot3", name="Mine", seed="7")
    with pytest.raises(ValueError):
        rig.reset_slot("slot3", "new_seed", test=True)
    assert rig.slots.get("slot3")["test"] is False


def test_the_owner_changing_a_test_world_makes_it_theirs() -> None:
    rig = _rig()
    rig.create_slot("slot4", {"name": "T", "test": True})
    assert rig.update_slot("slot4", {"name": "Our base"})["test"] is False


def test_the_default_is_saved_at_once_and_reported_in_status(tmp_path: Path) -> None:
    rig = _rig()
    rig.bds = bds.Bds(tmp_path)
    assert rig.status()["spawn_wreck"] is True
    assert rig.set_settings({"spawn_wreck": False})["spawn_wreck"] is False
    assert rig.status()["spawn_wreck"] is False
    assert rig.settings()["auto_update"] is False  # the other settings are left alone


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


def test_a_done_with_a_cause_is_a_world_passed_over(tmp_path: Path) -> None:
    rig = _rig()
    real = bds.Bds(tmp_path)
    rig.bds = real
    rig.slots.update("slot2", wreck="armed")
    real.world = "slot2"
    real._record(
        '[Scripting] [jbrain] {"ev":"wreck","phase":"done","cause":"not a new world"}'
    )
    rig.note_wreck()
    assert rig.slots.get("slot2")["wreck"] == "skipped"


def test_an_armed_worlds_build_phase_reaches_its_slot(tmp_path: Path) -> None:
    rig = _rig()
    real = bds.Bds(tmp_path)
    rig.bds = real

    def phase() -> Any:
        (rec,) = [s for s in rig.worlds_view()["slots"] if s["id"] == "slot2"]
        return rec["wreck_phase"]

    rig.create_slot("slot2", {"name": "New"})
    assert phase() is None
    real.world = "slot2"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"pod"}')
    rig.note_wreck()
    assert phase() == "pod"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"failed","cause":"x"}')
    rig.note_wreck()
    assert phase() == "failed"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"done"}')
    rig.note_wreck()
    assert phase() is None  # built: nothing in progress


def test_a_phase_from_before_a_reset_is_the_old_worlds(tmp_path: Path) -> None:
    _make_world("slot2")
    rig = _rig()
    real = bds.Bds(tmp_path)
    rig.bds = real
    rig.slots.update("slot2", seed="9", wreck="armed", created_at=1.0)
    real.world = "slot2"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"ship"}')
    rig.note_wreck()
    (rec,) = [s for s in rig.worlds_view()["slots"] if s["id"] == "slot2"]
    assert rec["wreck_phase"] == "ship"
    rig.reset_slot("slot2", "same_seed")
    (rec,) = [s for s in rig.worlds_view()["slots"] if s["id"] == "slot2"]
    assert rec["wreck"] == "armed" and rec["wreck_phase"] is None


def test_a_done_from_before_a_reset_is_the_old_worlds(tmp_path: Path) -> None:
    """The pack reports done, the owner resets that slot before the tracker reads it:
    the new, armed world must stay armed."""
    _make_world("slot2")
    rig = _rig()
    real = bds.Bds(tmp_path)
    rig.bds = real
    rig.slots.update("slot2", seed="9", wreck="armed")
    real.world = "slot2"
    real._record('[Scripting] [jbrain] {"ev":"wreck","phase":"done"}')
    rig.reset_slot("slot2", "same_seed")  # notes world_replaced, re-arms
    rig.note_wreck()
    assert rig.slots.get("slot2")["wreck"] == "armed"


def test_loading_an_empty_slot_generates_an_armed_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_world("world")
    rig = _rig()
    monkeypatch.setattr(rig, "snapshot", lambda label, **kw: {"name": label})
    monkeypatch.setattr(rig, "_apply_rules_when_up", lambda folder: None)
    rig.load_slot("slot3")
    assert rig.slots.get("slot3")["wreck"] == "armed"
    # Made now: a build phase left from the folder's last world isn't this one's.
    assert rig.slots.get("slot3")["created_at"]
    rig.set_settings({"spawn_wreck": False})
    rig.load_slot("slot4")
    assert rig.slots.get("slot4")["wreck"] is None


def test_restoring_another_worlds_backup_disarms_the_slot() -> None:
    _make_world("world")
    rig = _rig()
    snap = rig.snapshot("manual", folder="world")
    rig.create_slot("slot2", {"name": "Fresh"})
    assert rig.slots.get("slot2")["wreck"] == "armed"
    assert rig.restore_snapshot(snap["name"], "slot2")["wreck"] is None


def test_start_installs_the_wreck_before_the_boxs_own_pack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()
    rig.create_slot("slot2", {"name": "New"})
    rig._apply_slot(rig.slots.get("slot2"))
    monkeypatch.setattr(rig, "_apply_rules_when_up", lambda folder: None)
    rig._start()
    world = server.SERVER_DIR / "worlds" / "slot2"
    listed = {
        p["pack_id"]
        for p in json.loads((world / "world_behavior_packs.json").read_text())
    }
    jbrain = json.loads((MC / "jbrain-pack/manifest.json").read_text())["header"]
    assert listed == {WRECK_PACK_ID, jbrain["uuid"]}  # both reach the first session


def _node_pack(tmp_path: Path) -> Path:
    """The wreck pack's scripts beside a fake @minecraft/server, ready for node."""
    if shutil.which("node") is None:
        pytest.fail("node is needed to run the wreck pack's script (dev-setup has it)")
    root = tmp_path / "run"
    shutil.copytree(MC / "wreck-pack", root / "pack")
    structures.build(MC / "wreck", root / "pack")
    fake = root / "node_modules" / "@minecraft" / "server"
    fake.mkdir(parents=True)
    shutil.copy(MC / "wreck-sim" / "fake_server.mjs", fake / "index.mjs")
    (fake / "package.json").write_text(
        json.dumps(
            {"name": "@minecraft/server", "type": "module", "exports": "./index.mjs"}
        )
    )
    return root


def _node(root: Path, script: str) -> str:
    return subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _phases(out: str) -> list[str]:
    return [
        json.loads(line.split("[jbrain] ", 1)[1])["phase"]
        for line in out.splitlines()
        if "[jbrain] " in line
    ]


@pytest.mark.parametrize(
    ("before", "ticks"),
    [
        # SW1 passed it over: a world armed before the build shipped and played since.
        ('{"stage":"done","skipped":true}', 0),
        # No record, but it has run for an hour: not a new world either.
        (None, 72000),
    ],
)
def test_a_world_that_is_not_new_is_never_built_on(
    tmp_path: Path, before: str | None, ticks: int
) -> None:
    root = _node_pack(tmp_path)
    preset = f"sim.props.set('jbrain:wreck', {json.dumps(before)});" if before else ""
    out = _node(
        root,
        f"""
        import {{ sim }} from "@minecraft/server";
        let placed = 0;
        sim.reset(() => "minecraft:air", {{ x: 0, z: 0 }}, () => placed++);
        {preset}
        sim.setTime({ticks});
        await import("./pack/scripts/main.js");
        sim.load();
        console.log("PLACED " + placed);
        console.log("STATE " + sim.props.get("jbrain:wreck"));
        """,
    )
    assert "PLACED 0" in out
    assert _phases(out) == ["loaded", "done"]  # the wrapper still disarms it
    assert json.loads(out.split("STATE ", 1)[1].splitlines()[0])["stage"] == "done"


def test_the_planning_rules_hold(tmp_path: Path) -> None:
    """plan.js's own unit tests (node --test): site choice, materials, crater, canopy,
    apron, ship height, kit."""
    _node_pack(tmp_path)  # fails clearly if node is missing
    result = subprocess.run(
        ["node", "--test", str(MC / "wreck-sim" / "plan.test.mjs")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


SIM_SCENARIOS = [
    "plains", "forest", "jungle", "desert", "snowy", "mountainside", "badlands",
    "swamp", "low_swamp", "mangrove", "island", "ocean", "high_plateau",
    "place_fails_once",
    "peaks_under_ship", "reloaded", "short_ship_area", "slow_and_stuck",
    "already_played",
    "resume_after_1pct",
    "resume_after_30pct",
    "resume_after_60pct", "resume_after_90pct",
]  # fmt: skip


def test_the_crash_site_comes_down_right_in_every_kind_of_world(tmp_path: Path) -> None:
    """The real build script against a synthetic world per kind of ground a spawn can
    be on: the pod whole and supported, the hatch open onto walkable ground, spawn on
    it, the parachute on the ground, the kit stocked, the ship clear of the terrain and
    under the build limit, and no ticking area left behind."""
    root = _node_pack(tmp_path)
    shutil.copy(MC / "wreck-sim" / "sim.mjs", root / "sim.mjs")
    out = subprocess.run(
        ["node", "sim.mjs", "pack", str(MC / "wreck")],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    ).stdout
    runs = [json.loads(line) for line in out.splitlines()]
    assert [r["scenario"] for r in runs] == SIM_SCENARIOS
    results = {r["scenario"]: r for r in runs}
    failed = {
        name: [k for k, ok in r["checks"].items() if not ok]
        for name, r in results.items()
        if not r["ok"]
    }
    assert not failed
    assert results["ocean"]["site"]["splash"] is True
    assert not any(
        r["site"]["splash"]
        for n, r in results.items()
        if n not in ("ocean", "already_played", "slow_and_stuck")
    )
    assert "site" not in results["already_played"]  # passed over: nothing built
    assert "site" not in results["slow_and_stuck"]  # gave up in time, built nothing
    # A spawn too high for the ship's usual height: capped under the build limit.
    assert results["high_plateau"]["ship"]["y"] + 64 <= 319
    # Stopped dead mid-build and resumed: the same world as an uninterrupted build.
    for n in SIM_SCENARIOS:
        if n.startswith("resume_after_"):
            assert results[n]["checks"]["was_stopped"]  # really cut off part-way
            assert results[n]["checks"]["same_world_as_uninterrupted"]
    plains = results["plains"]["ship"]["z"]
    assert results["peaks_under_ship"]["ship"]["z"] != plains  # slid clear of the peaks


class _WatchBds(_FakeBds):
    def __init__(self, events: list[dict[str, Any]], players: dict[str, Any]) -> None:
        super().__init__()
        self._events = events
        self.players = players
        self.running = True
        self.calls: list[str] = []

    def wait_running(self, _timeout_s: float) -> bool:
        return True

    def events_after(self, _after: int) -> list[dict[str, Any]]:
        return [e for e in self._events if e["id"] > _after]

    def stop(self) -> None:
        self.calls.append("stop")

    def start(self) -> None:
        self.calls.append("start")


@pytest.mark.parametrize(
    ("events", "players", "restarted"),
    [
        ([], {}, True),  # the pack never reported: restart once, before anyone plays
        ([{"id": 5, "kind": "wreck", "phase": "loaded"}], {}, False),
        ([], {"Steve": {"xuid": "1"}}, False),  # someone's already in: leave it
    ],
)
def test_a_new_world_that_came_up_without_its_pack_restarts_once(
    monkeypatch: pytest.MonkeyPatch,
    events: list[dict[str, Any]],
    players: dict[str, Any],
    restarted: bool,
) -> None:
    monkeypatch.setattr(server, "WRECK_WAIT_S", 0.05)
    monkeypatch.setattr(server, "WARN_S", 0)
    rig = _rig()
    rig.set_settings({"auto_update": True})  # a plain restart regardless: no update
    rig.create_slot("slot2", {"name": "New"})
    rig._apply_slot(rig.slots.get("slot2"))
    monkeypatch.setattr(rig, "_apply_rules_when_up", lambda folder: None)
    fake = _WatchBds(events, players)
    rig.bds = fake
    monkeypatch.setattr(
        rig, "version_info", lambda **kw: pytest.fail("no update check")
    )
    rig._watch_wreck("slot2", 0)
    assert fake.calls == (["stop", "start"] if restarted else [])
    assert rig._life.acquire(blocking=False)  # and the lock is free again


def test_the_restart_stands_down_while_another_job_holds_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "WRECK_WAIT_S", 0.05)
    rig = _rig()
    rig.create_slot("slot2", {"name": "New"})
    rig._apply_slot(rig.slots.get("slot2"))
    fake = _WatchBds([], {})
    rig.bds = fake
    assert rig._life.acquire(blocking=False)  # say, a world switch in progress
    rig._watch_wreck("slot2", 0)
    assert fake.calls == []


def test_the_wreck_pack_is_its_own_pack_on_stable_scripting() -> None:
    manifest = json.loads((MC / "wreck-pack/manifest.json").read_text())
    jbrain = json.loads((MC / "jbrain-pack/manifest.json").read_text())
    assert manifest["header"]["uuid"] != jbrain["header"]["uuid"]
    deps = {d["module_name"]: d["version"] for d in manifest["dependencies"]}
    assert deps == {"@minecraft/server": "2.0.0"}  # stable, no experiments
