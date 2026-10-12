"""Wormhole gates in the always-installed `jbrain` packs
(docs/plans/MINECRAFT_BEDROCK_PLAN.md §M12): the Wormhole Seed, its recipe and icon, the
gate's surface block and its animated texture, and wormhole.js run under node against
the fake @minecraft/server."""

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
ART = MC.parents[1] / "docs" / "mocks" / "minecraft-items" / "a"


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def _png(path: Path) -> tuple[int, int, int]:
    """Width, height and colour type (6 is RGBA) from a PNG's header."""
    head = path.read_bytes()[:26]
    assert head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR"
    return int.from_bytes(head[16:20]), int.from_bytes(head[20:24]), head[25]


def test_the_seed_is_crafted_from_a_power_pack_chorus_fruit_and_pearls() -> None:
    """The chorus seed (owner, 2026-10-11): a Power Pack in the centre, popped chorus
    fruit on the sides, Ender Pearls in the corners; one to a stack."""
    item = _json(PACK / "items" / "wormhole_seed.json")["minecraft:item"]
    assert item["description"]["identifier"] == "jbrain:wormhole_seed"
    assert item["components"]["minecraft:max_stack_size"] == 1
    assert item["components"]["minecraft:display_name"] == {"value": "Wormhole Seed"}
    shaped = _json(PACK / "recipes" / "wormhole_seed.json")["minecraft:recipe_shaped"]
    assert shaped["tags"] == ["crafting_table"]
    assert shaped["pattern"] == ["EFE", "FPF", "EFE"]
    assert shaped["key"] == {
        "E": {"item": "minecraft:ender_pearl"},
        "F": {"item": "minecraft:popped_chorus_fruit"},
        "P": {"item": "jbrain:power_pack"},
    }
    assert shaped["result"] == {"item": "jbrain:wormhole_seed", "count": 1}


def test_the_surface_is_a_walk_through_unbreakable_glowing_block() -> None:
    block = _json(PACK / "blocks" / "wormhole.json")["minecraft:block"]
    assert block["description"]["identifier"] == "jbrain:wormhole"
    assert block["description"]["menu_category"]["category"] == "none"
    c = block["components"]
    # Players walk into it (the script teleports them); only the frame breaks a gate.
    assert c["minecraft:collision_box"] is False
    assert c["minecraft:destructible_by_mining"] is False
    assert c["minecraft:destructible_by_explosion"] is False
    assert c["minecraft:light_emission"] > 0
    assert c["minecraft:material_instances"]["*"]["texture"] == "jbrain_wormhole"
    assert c["minecraft:material_instances"]["*"]["render_method"] == "blend"


def test_the_art_ships_as_drawn_and_the_surface_is_a_flipbook() -> None:
    atlas = _json(RP / "textures" / "item_texture.json")["texture_data"]
    seed = RP / f"{atlas['jbrain_wormhole_seed']['textures']}.png"
    assert seed.read_bytes() == (ART / "wormhole_seed.png").read_bytes()
    terrain = _json(RP / "textures" / "terrain_texture.json")
    assert terrain["texture_name"] == "atlas.terrain"
    path = terrain["texture_data"]["jbrain_wormhole"]["textures"]
    strip = RP / f"{path}.png"
    assert strip.read_bytes() == (ART / "wormhole.png").read_bytes()
    flip = _json(RP / "textures" / "flipbook_textures.json")
    (mine,) = [f for f in flip if f["atlas_tile"] == "jbrain_wormhole"]
    assert mine["flipbook_texture"] == path
    width, height, colour = _png(strip)
    assert (width, colour) == (16, 6) and height % 16 == 0 and height > 16
    assert _png(seed) == (16, 16, 6)


def test_main_only_starts_the_gates() -> None:
    """The gates live in their own module; main.js imports and starts them."""
    main = (PACK / "scripts" / "main.js").read_text()
    assert 'import { initWormholes } from "./wormhole.js";' in main
    assert main.count("initWormholes(") == 1
    assert "jb:wormhole" not in main


def _node(tmp_path: Path, *tests: str) -> Path:
    if shutil.which("node") is None:
        pytest.fail("node is needed to run the jbrain pack's script (dev-setup has it)")
    root = tmp_path / "run"
    shutil.copytree(PACK, root / "pack")
    # main.js loads every module, so server-ui's stand-in too (transport's form).
    for module, source in (
        ("server", "fake_server.mjs"),
        ("server-ui", "fake_server_ui.mjs"),
    ):
        fake = root / "node_modules" / "@minecraft" / module
        fake.mkdir(parents=True)
        shutil.copy(SIM / source, fake / "index.mjs")
        (fake / "package.json").write_text(
            json.dumps(
                {
                    "name": f"@minecraft/{module}",
                    "type": "module",
                    "exports": "./index.mjs",
                }
            )
        )
    for name in tests:
        shutil.copy(SIM / name, root / name)
    return root


def _run(path: Path, cwd: Path | None = None) -> None:
    result = subprocess.run(
        ["node", "--test", str(path)], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_the_gate_rules_hold(tmp_path: Path) -> None:
    """Frame detection on either axis with or without corners, reach and ownership, the
    aim plan's charges, strict parsing, and the seed's unlock."""
    _node(tmp_path)  # fails clearly if node is missing
    _run(SIM / "wormhole_rules.test.mjs")


def test_the_pack_runs_the_gates(tmp_path: Path) -> None:
    """wormhole.js against the fake server: planting, opening and re-aiming, the Power
    Pack charged only on a real change, anyone stepping through, a blocked landing,
    and a broken ring losing its seed."""
    root = _node(tmp_path, "wormhole.test.mjs")
    _run(root / "wormhole.test.mjs", cwd=root)
