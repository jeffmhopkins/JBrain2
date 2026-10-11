"""Dave's item guide (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M19): one entry per BUILT Old
Maker item, and in game only the entries the asker has unlocked, so a locked item is not
in the model's context at all. The door's use of it is in `test_minecraft_door.py`.
"""

from __future__ import annotations

import hashlib

from jbrain.minecraft import guide


def test_the_guide_is_pinned_to_its_version() -> None:
    """The entries are prompt material: a wording change bumps the version with it."""
    assert guide.VERSION == "agent-minecraft-dave-guide-v2"
    body = guide._PROMPT.body  # noqa: SLF001 — the pin is on the file's exact text
    assert hashlib.sha256(body.encode()).hexdigest() == (
        "6d43fb830c6ca3c0fb78966b0d1b7878f0b73b65d5883ad9a005aee6faeda8c2"
    )


def test_only_built_items_have_entries() -> None:
    """The Tricorder and the Power Pack are built; its keep-your-things use is built (a
    per-world switch), and so are wormhole gates (§M12); transport, the No-Spawn Beacon
    and the Replicator are not, so the guide never describes them."""
    assert set(guide.ENTRIES) == {
        "intro_game",
        "intro_owner",
        "tricorder",
        "power_pack",
        "power_pack_keep",
        "power_pack_unused",
        "wormhole_seed",
    }
    whole = "\n".join(guide.ENTRIES.values()).lower()
    for unbuilt in ("replicator", "no-spawn", "echo shard", "transport"):
        assert unbuilt not in whole, unbuilt
    # The canon's recipe and rules, as the lore page says them.
    assert "eight Ender Pearls in a ring around one Blaze Powder" in guide.ENTRIES["power_pack"]
    assert "only works for its owner" in guide.ENTRIES["tricorder"]
    assert "their own Tricorder" in guide.ENTRIES["power_pack_keep"]
    seed = " ".join(guide.ENTRIES["wormhole_seed"].split())
    for canon in (
        "4 popped chorus fruit",
        "4 Ender Pearls in the corners",
        "blocks of diamond",
        "within 4 blocks",
        "one of their own saved places",
        "only goes one way",
        "Each re-aim burns one Power Pack",
        "the seed burns out",
        "anyone can step in",
    ):
        assert canon in seed, canon


def test_known_is_read_strictly_and_always_has_the_tricorder() -> None:
    assert guide.parse_known(None) == {"tricorder"}
    assert guide.parse_known("power_pack") == {"tricorder"}
    assert guide.parse_known(["power_pack", 3, "replicator", "power_pack_uses"]) == {
        "tricorder",
        "power_pack",
        "power_pack_uses",
    }


def test_each_unlock_opens_its_own_entry_and_no_other() -> None:
    assert guide.entries_for({"tricorder"}, keep_switch=True) == ["tricorder"]
    assert guide.entries_for({"power_pack"}, keep_switch=True) == ["tricorder", "power_pack"]
    assert guide.entries_for({"power_pack", "power_pack_uses"}, keep_switch=True) == [
        "tricorder",
        "power_pack",
        "power_pack_keep",
    ]
    # A world without the switch has no use to describe.
    assert guide.entries_for({"power_pack_uses"}, keep_switch=False) == [
        "tricorder",
        "power_pack",
        "power_pack_unused",
    ]
    fresh = guide.for_player({"tricorder"}, keep_switch=True)
    assert "Tricorder" in fresh
    for locked in ("Power Pack", "Ender Pearl", "Blaze Powder", "field"):
        assert locked not in fresh, locked


def test_the_owner_sees_every_built_item() -> None:
    owner = guide.for_owner()
    for heading in ("### The Tricorder", "### The Power Pack", "keeping your things"):
        assert heading in owner
    assert "Nothing in this world yet" not in owner
    assert "can make" not in owner.split("\n", 1)[0]  # the owner's intro, not the game's


def test_the_wormhole_seed_opens_its_entry_and_a_pack_is_then_never_useless() -> None:
    """The pack reports `wormhole_seed` once a player who made a Power Pack holds popped
    chorus fruit. A re-aim burns a pack, so in a world without the keep switch the
    "does nothing yet" entry gives way to the gates."""
    assert guide.parse_known(["power_pack_uses", "wormhole_seed"]) >= {"wormhole_seed"}
    known = {"power_pack", "power_pack_uses", "wormhole_seed"}
    assert guide.entries_for(known, keep_switch=True) == [
        "tricorder",
        "power_pack",
        "power_pack_keep",
        "wormhole_seed",
    ]
    assert guide.entries_for(known, keep_switch=False) == [
        "tricorder",
        "power_pack",
        "wormhole_seed",
    ]
    assert "wormhole" not in guide.for_player({"power_pack_uses"}, keep_switch=True).lower()
    assert "### The Wormhole Seed" in guide.for_owner()
