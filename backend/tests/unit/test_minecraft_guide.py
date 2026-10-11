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
        "ac6dd12200f15d9227204765a3aba54a77ecce365b7db4e0850e8b5db7bb27b0"
    )


def test_only_built_items_have_entries() -> None:
    """The Tricorder, the Power Pack and the No-Spawn Beacon are built; the pack's
    keep-your-things use is built (a per-world switch); transport, wormholes and the
    Replicator are not, so the guide never describes them."""
    assert set(guide.ENTRIES) == {
        "intro_game",
        "intro_owner",
        "tricorder",
        "power_pack",
        "power_pack_keep",
        "power_pack_unused",
        "no_spawn_beacon",
    }
    whole = "\n".join(guide.ENTRIES.values()).lower()
    for unbuilt in ("replicator", "wormhole", "chorus", "transport"):
        assert unbuilt not in whole, unbuilt
    # The canon's recipe and rules, as the lore page says them.
    assert "eight Ender Pearls in a ring around one Blaze Powder" in guide.ENTRIES["power_pack"]
    assert "only works for its owner" in guide.ENTRIES["tricorder"]
    assert "their own Tricorder" in guide.ENTRIES["power_pack_keep"]
    beacon = guide.ENTRIES["no_spawn_beacon"]
    assert "a Power Pack in the middle of a crafting table, surrounded by eight echo" in beacon
    assert "so does the Warden" in beacon and "the Ender Dragon and the Wither" in beacon


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


def test_the_beacon_opens_only_on_its_own_unlock() -> None:
    """The pack sends `no_spawn_beacon` once a player who has made a Power Pack holds an
    echo shard (rules.js `UNLOCK_NEEDS`); until then the beacon isn't in the turn."""
    assert "no_spawn_beacon" in guide.parse_known(["no_spawn_beacon"])
    made_a_pack = {"power_pack", "power_pack_uses"}
    assert "no_spawn_beacon" not in guide.entries_for(made_a_pack, keep_switch=True)
    assert "No-Spawn" not in guide.for_player(made_a_pack, keep_switch=True)
    assert guide.entries_for({*made_a_pack, "no_spawn_beacon"}, keep_switch=False)[-1] == (
        "no_spawn_beacon"
    )
    assert "### The No-Spawn Beacon" in guide.for_owner()
