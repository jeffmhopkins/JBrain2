"""The No-Spawn Beacon (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M16): its block, its
recipe and its faces in the `jbrain` packs, and its script's rules run under node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MC = Path(__file__).resolve().parents[2] / "deploy" / "minecraft"
PACK = MC / "jbrain-pack"
RP = MC / "jbrain-rp"
DOCS_ICONS = MC.parents[1] / "docs" / "mocks" / "minecraft-items" / "a"


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_the_recipe_is_a_power_pack_ringed_by_eight_echo_shards() -> None:
    shaped = _json(PACK / "recipes" / "no_spawn_beacon.json")["minecraft:recipe_shaped"]
    assert shaped["tags"] == ["crafting_table"]
    assert shaped["pattern"] == ["SSS", "SPS", "SSS"]
    assert shaped["key"] == {
        "S": {"item": "minecraft:echo_shard"},
        "P": {"item": "jbrain:power_pack"},
    }
    assert shaped["unlock"] == [{"item": "minecraft:echo_shard"}]
    assert shaped["result"] == {"item": "jbrain:no_spawn_beacon", "count": 1}


def test_the_block_breaks_like_any_block_and_wears_set_a() -> None:
    """Anyone can break it and it drops itself (canon): a custom block with no loot
    table drops itself, so the JSON must not name one."""
    block = _json(PACK / "blocks" / "no_spawn_beacon.json")["minecraft:block"]
    assert block["description"]["identifier"] == "jbrain:no_spawn_beacon"
    comps = block["components"]
    assert "minecraft:loot" not in comps
    assert comps["minecraft:destructible_by_mining"]["seconds_to_destroy"] > 0
    atlas = _json(RP / "textures" / "terrain_texture.json")
    assert atlas["texture_name"] == "atlas.terrain"
    faces = comps["minecraft:material_instances"]
    assert set(faces) == {"*", "up", "down"}
    for face in faces.values():
        key = face["texture"]
        png = RP / f"{atlas['texture_data'][key]['textures']}.png"
        name = key.removeprefix("jbrain_")
        assert png.read_bytes() == (DOCS_ICONS / f"{name}.png").read_bytes()


def test_main_only_starts_the_beacon() -> None:
    """The beacon's logic lives in its own module; main.js hands it the game API."""
    main = (PACK / "scripts" / "main.js").read_text()
    assert 'import { initBeacon } from "./beacon.js";' in main
    assert main.count("initBeacon(") == 1
    beacon = (PACK / "scripts" / "beacon.js").read_text()
    assert "@minecraft/server" not in beacon


def test_the_beacon_script_holds() -> None:
    """beacon.test.mjs: the keep/remove table (the Warden goes, the Dragon and the
    Wither stay), the 3x3 chunk math at negative coordinates, placement and break
    records, the sweep catching a teleported enderman, and the guide's unlock."""
    if shutil.which("node") is None:
        pytest.fail("node is needed to run the jbrain pack's script (dev-setup has it)")
    result = subprocess.run(
        ["node", "--test", str(MC / "jbrain-sim" / "beacon.test.mjs")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
