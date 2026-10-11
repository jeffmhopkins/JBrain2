"""The Replicator in the `jbrain` packs (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M15; the
item art and its placer-item design: docs/mocks/minecraft-items/README.md, set A): the
recipe, the two placer items, the block, its icons and faces, and replicator.js run
under node against a fake @minecraft/server with blocks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MC = Path(__file__).resolve().parents[2] / "deploy" / "minecraft"
PACK = MC / "jbrain-pack"
RP = MC / "jbrain-rp"
SIM = MC / "jbrain-sim"
DOCS_ICONS = MC.parents[1] / "docs" / "mocks" / "minecraft-items" / "a"


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_eight_power_packs_round_a_furnace_make_a_blank_replicator() -> None:
    shaped = _json(PACK / "recipes" / "replicator.json")["minecraft:recipe_shaped"]
    assert shaped["tags"] == ["crafting_table"]
    assert shaped["pattern"] == ["PPP", "PFP", "PPP"]
    assert shaped["key"] == {
        "P": {"item": "jbrain:power_pack"},
        "F": {"item": "minecraft:furnace"},
    }
    assert shaped["unlock"] == [{"item": "jbrain:power_pack"}]
    assert shaped["result"] == {"item": "jbrain:replicator", "count": 1}


def test_both_items_place_the_block_and_draw_set_as_icons() -> None:
    """The blank is the block's own item (`replace_block_item`); the programmed one is
    a second placer only so its slot can draw the lit sprite, and is never in the
    creative menu."""
    atlas = _json(RP / "textures" / "item_texture.json")["texture_data"]
    for name, replaces, menu in (
        ("replicator", True, "items"),
        ("replicator_programmed", None, "none"),
    ):
        item = _json(PACK / "items" / f"{name}.json")["minecraft:item"]
        assert item["description"]["identifier"] == f"jbrain:{name}"
        assert item["description"]["menu_category"]["category"] == menu
        comps = item["components"]
        assert comps["minecraft:max_stack_size"] == 1  # item properties need it
        placer = comps["minecraft:block_placer"]
        assert placer["block"] == "jbrain:replicator"
        assert placer.get("replace_block_item") is replaces
        icon = comps["minecraft:icon"]
        assert icon == f"jbrain_{name}"
        png = RP / f"{atlas[icon]['textures']}.png"
        assert png.read_bytes() == (DOCS_ICONS / f"{name}.png").read_bytes()


def test_the_block_faces_the_placer_and_lights_up_when_programmed() -> None:
    block = _json(PACK / "blocks" / "replicator.json")["minecraft:block"]
    desc = block["description"]
    assert desc["identifier"] == "jbrain:replicator"
    assert desc["states"] == {
        "jbrain:programmed": [False, True],
        "jbrain:working": [False, True],
    }
    trait = desc["traits"]["minecraft:placement_direction"]
    assert trait["enabled_states"] == ["minecraft:cardinal_direction"]
    comps = block["components"]
    # The script's component, its 10-second tick, and the drop the script replaces.
    assert "jbrain:replicator" in comps
    assert comps["minecraft:tick"] == {"interval_range": [200, 200], "looping": True}
    loot = _json(PACK / comps["minecraft:loot"])
    assert loot == {"pools": []}
    assert comps["minecraft:destructible_by_explosion"] is False
    assert comps["minecraft:movable"] == {"movement_type": "immovable"}
    rotations = {}
    programmed = None
    working: dict = {}
    for perm in block["permutations"]:
        if "cardinal_direction" in perm["condition"]:
            way = perm["condition"].split("== '")[1].rstrip("'")
            rotations[way] = perm["components"]["minecraft:transformation"]["rotation"]
        else:
            if "working" in perm["condition"]:
                working = perm["components"]
            else:
                programmed = perm["components"]["minecraft:material_instances"]
    assert rotations == {
        "north": [0, 0, 0],
        "west": [0, 90, 0],
        "south": [0, 180, 0],
        "east": [0, -90, 0],
    }
    blank = comps["minecraft:material_instances"]
    assert programmed is not None
    assert blank["north"]["texture"] == "jbrain_replicator_front"
    assert programmed["north"]["texture"] == "jbrain_replicator_front_programmed"
    assert blank["up"]["texture"] == "jbrain_replicator_top"
    assert programmed["up"]["texture"] == "jbrain_replicator_top_programmed"
    assert blank["*"] == programmed["*"]
    # Working (owner, 2026-10-11): the pulsing front and top, and it gives off light.
    lit = working["minecraft:material_instances"]
    assert lit["north"]["texture"] == "jbrain_replicator_front_working"
    assert lit["up"]["texture"] == "jbrain_replicator_top_working"
    assert working["minecraft:light_emission"] == 10
    assert "minecraft:light_emission" not in comps
    flipbooks = json.loads((RP / "textures" / "flipbook_textures.json").read_text())
    ours_fb = {f["atlas_tile"]: f for f in flipbooks if "replicator" in f["atlas_tile"]}
    assert set(ours_fb) == {
        "jbrain_replicator_front_working",
        "jbrain_replicator_top_working",
    }
    terrain = _json(RP / "textures" / "terrain_texture.json")
    assert terrain["texture_name"] == "atlas.terrain"
    ours = {k: v for k, v in terrain["texture_data"].items() if "replicator" in k}
    for key, entry in ours.items():
        face = key.removeprefix("jbrain_")
        png = RP / f"{entry['textures']}.png"
        if key in ours_fb:  # 8 frames stacked: 16 x 128
            assert ours_fb[key]["flipbook_texture"] == entry["textures"]
        assert png.read_bytes() == (DOCS_ICONS / f"{face}.png").read_bytes()
    used = {
        m["texture"] for m in [*blank.values(), *programmed.values(), *lit.values()]
    }
    assert used == set(ours)


def test_main_only_starts_the_replicator() -> None:
    """Its logic is its own module; main.js imports it and calls it once."""
    main = (PACK / "scripts" / "main.js").read_text()
    assert 'import { initReplicator } from "./replicator.js";' in main
    assert main.count("initReplicator()") == 1
    assert "jb:replicator" not in main


def test_the_replicator_script(tmp_path: Path) -> None:
    """replicator.js and its rules: programming (exactly 512; too few, a 16-stacker, an
    unstackable; reprogramming), placing and breaking (the named item round-trips),
    the run (cadence, fuel, a full output, an unloaded chunk) and the §M19 unlock."""
    if shutil.which("node") is None:
        pytest.fail("node is needed to run the jbrain pack's script (dev-setup has it)")
    root = tmp_path / "run"
    shutil.copytree(PACK, root / "pack")
    for module, sources in (
        ("server", {"base.mjs": "fake_server.mjs", "index.mjs": "replicator_fake.mjs"}),
        ("server-ui", {"index.mjs": "fake_server_ui.mjs"}),
    ):
        fake = root / "node_modules" / "@minecraft" / module
        fake.mkdir(parents=True)
        for name, source in sources.items():
            shutil.copy(SIM / source, fake / name)
        (fake / "package.json").write_text(
            json.dumps(
                {
                    "name": f"@minecraft/{module}",
                    "type": "module",
                    "exports": "./index.mjs",
                }
            )
        )
    shutil.copy(SIM / "replicator.test.mjs", root / "replicator.test.mjs")
    result = subprocess.run(
        ["node", "--test", str(root / "replicator.test.mjs")],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
