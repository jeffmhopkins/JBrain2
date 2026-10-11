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
        "57e3a815efb31126254a78e432f0dad7e7dc2f6990460c2c035d346cbc64fb5a"
    )


def test_only_built_items_have_entries() -> None:
    """The Tricorder, the Power Pack and the Replicator are built; the pack's
    keep-your-things use is built (a per-world switch); transport, the No-Spawn Beacon and
    wormholes are not, so the guide never describes them."""
    assert set(guide.ENTRIES) == {
        "intro_game",
        "intro_owner",
        "tricorder",
        "power_pack",
        "power_pack_keep",
        "power_pack_unused",
        "replicator",
    }
    whole = "\n".join(guide.ENTRIES.values()).lower()
    for unbuilt in ("wormhole", "no-spawn", "echo shard", "chorus", "transport"):
        assert unbuilt not in whole, unbuilt
    # The canon's recipe and rules, as the lore page says them.
    assert "eight Ender Pearls in a ring around one Blaze Powder" in guide.ENTRIES["power_pack"]
    assert "only works for its owner" in guide.ENTRIES["tricorder"]
    assert "their own Tricorder" in guide.ENTRIES["power_pack_keep"]


def test_known_is_read_strictly_and_always_has_the_tricorder() -> None:
    assert guide.parse_known(None) == {"tricorder"}
    assert guide.parse_known("power_pack") == {"tricorder"}
    assert guide.parse_known(["power_pack", 3, "wormhole", "power_pack_uses"]) == {
        "tricorder",
        "power_pack",
        "power_pack_uses",
    }
    assert "replicator" in guide.parse_known(["replicator"])


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
    for heading in (
        "### The Tricorder",
        "### The Power Pack",
        "keeping your things",
        "### The Replicator",
    ):
        assert heading in owner
    assert "Nothing in this world yet" not in owner
    assert "can make" not in owner.split("\n", 1)[0]  # the owner's intro, not the game's


def test_the_replicator_entry_opens_with_8_power_packs_and_says_the_canon() -> None:
    """§M15: Dave explains the Replicator once someone holds 8 Power Packs (canon); the
    pack reports it as `replicator`."""
    assert "replicator" not in guide.entries_for({"power_pack_uses"}, keep_switch=True)
    assert (
        guide.entries_for({"power_pack", "power_pack_uses", "replicator"}, keep_switch=True)[-1]
        == "replicator"
    )
    entry = guide.ENTRIES["replicator"]
    for canon in ("eight Power Packs", "8 full stacks (512)", "stack to 64", "about every 10"):
        assert canon in entry, canon
    assert "Replicator" not in guide.for_player({"power_pack_uses"}, keep_switch=True)
