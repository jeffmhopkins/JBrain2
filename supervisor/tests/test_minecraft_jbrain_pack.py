"""The always-installed `jbrain` packs (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M9, §M10,
§DW1, §3c): the crew's items and their icons, and the pack script's rules, run under
node against a fake @minecraft/server as the wreck pack's are."""

from __future__ import annotations

import json
import re
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


def _item(name: str) -> dict:
    return _json(PACK / "items" / f"{name}.json")["minecraft:item"]


def test_each_item_draws_the_owners_chosen_icon_from_the_pushed_pack() -> None:
    """Set A with the ping Tricorder (docs/mocks/minecraft-items, owner 2026-10-11): the
    exact PNGs, under the keys the item JSON names, in the pack the behavior pack
    needs."""
    atlas = _json(RP / "textures" / "item_texture.json")
    assert atlas["texture_name"] == "atlas.items"
    for name in ("tricorder", "power_pack"):
        icon = _item(name)["components"]["minecraft:icon"]
        assert icon == f"jbrain_{name}"
        png = RP / f"{atlas['texture_data'][icon]['textures']}.png"
        assert png.read_bytes() == (DOCS_ICONS / f"{name}.png").read_bytes()
    rp = _json(RP / "manifest.json")["header"]
    deps = _json(PACK / "manifest.json")["dependencies"]
    assert {"uuid": rp["uuid"], "version": rp["version"]} in deps


def test_the_items_and_the_power_pack_recipe() -> None:
    tricorder = _item("tricorder")
    assert tricorder["description"]["identifier"] == "jbrain:tricorder"
    # Beamed down, never crafted or picked from the creative menu (DW1).
    assert tricorder["description"]["menu_category"]["category"] == "none"
    assert tricorder["components"]["minecraft:max_stack_size"] == 1
    pack = _item("power_pack")
    assert pack["description"]["identifier"] == "jbrain:power_pack"
    assert pack["components"]["minecraft:max_stack_size"] == 16
    assert pack["components"]["minecraft:glint"] is True
    shaped = _json(PACK / "recipes" / "power_pack.json")["minecraft:recipe_shaped"]
    assert shaped["tags"] == ["crafting_table"]
    # Eight Ender Pearls round one Blaze Powder (owner, 2026-10-11).
    assert shaped["pattern"] == ["PPP", "PBP", "PPP"]
    assert shaped["key"] == {
        "P": {"item": "minecraft:ender_pearl"},
        "B": {"item": "minecraft:blaze_powder"},
    }
    assert shaped["unlock"] == [{"item": "minecraft:ender_pearl"}]
    assert shaped["result"] == {"item": "jbrain:power_pack", "count": 1}
    assert not list(PACK.glob("recipes/tricorder*"))
    # Items and recipes are data: the pack declares a data module beside its script.
    modules = {m["type"] for m in _json(PACK / "manifest.json")["modules"]}
    assert modules == {"data", "script"}


def test_one_pack_defines_each_item() -> None:
    """Two packs defining one identifier collide; the test kit carries none any more."""
    assert not (MC / "probe-pack" / "items").exists()
    assert not (MC / "probe-pack" / "recipes").exists()
    probe = (MC / "probe-pack" / "scripts" / "main.js").read_text()
    assert "jb:waypoint" not in probe and "setActionBar" not in probe


def test_the_tricorder_is_locked_and_kept_by_the_script() -> None:
    """lockMode and keepOnDeath can't be set in item JSON; the script sets both on
    every copy it makes, on stable API only."""
    main = (PACK / "scripts" / "main.js").read_text()
    assert "item.lockMode = ItemLockMode.inventory;" in main
    assert "item.keepOnDeath = true;" in main
    manifest = _json(PACK / "manifest.json")
    server = next(
        d
        for d in manifest["dependencies"]
        if d.get("module_name") == "@minecraft/server"
    )
    assert not re.search(r"beta|alpha", server["version"])


def test_daves_lines_live_in_one_block_marked_with_their_sign_off() -> None:
    rules = (PACK / "scripts" / "rules.js").read_text()
    block = rules.split("export const LINES = {", 1)[0]
    assert "Signed off by the owner (2026-10-11): `noSignal` and `notTuned`" in block
    assert "DRAFTS" in block
    main = (PACK / "scripts" / "main.js").read_text()
    assert "kssht" not in main and "nearly lost you" not in main  # only via LINES


def _node_root(tmp_path: Path) -> Path:
    if shutil.which("node") is None:
        pytest.fail("node is needed to run the jbrain pack's script (dev-setup has it)")
    root = tmp_path / "run"
    shutil.copytree(PACK, root / "pack")
    fake = root / "node_modules" / "@minecraft" / "server"
    fake.mkdir(parents=True)
    shutil.copy(MC / "jbrain-sim" / "fake_server.mjs", fake / "index.mjs")
    (fake / "package.json").write_text(
        json.dumps(
            {"name": "@minecraft/server", "type": "module", "exports": "./index.mjs"}
        )
    )
    shutil.copy(MC / "jbrain-sim" / "pack.test.mjs", root / "pack.test.mjs")
    return root


def _node_test(path: Path, cwd: Path | None = None) -> None:
    result = subprocess.run(
        ["node", "--test", str(path)], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_the_pure_rules_hold(tmp_path: Path) -> None:
    """rules.js's own tests: the arrow's octants and wrap, ownership, the death table,
    the drop fates, the pack to burn, XP, the void spot, parsing, and the lines."""
    _node_root(tmp_path)  # fails clearly if node is missing
    _node_test(MC / "jbrain-sim" / "rules.test.mjs")


def test_the_pack_script_enforces_the_rules(tmp_path: Path) -> None:
    """main.js against the fake server: the Tricorder beamed and bound, re-beamed on
    waking, owner-only arrow, no Tricorder no Dave, and the Power Pack charm's
    deaths."""
    root = _node_root(tmp_path)
    _node_test(root / "pack.test.mjs", cwd=root)
