"""Dave's item guide (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M19): one entry per BUILT Old
Maker item, and in game only the entries the asker has unlocked, so a locked item is not
in the model's context at all. The door's use of it is in `test_minecraft_door.py`.
"""

from __future__ import annotations

import hashlib

from jbrain.minecraft import guide


def test_the_guide_is_pinned_to_its_version() -> None:
    """The entries are prompt material: a wording change bumps the version with it."""
    assert guide.VERSION == "agent-minecraft-dave-guide-v11"
    body = guide._PROMPT.body  # noqa: SLF001 — the pin is on the file's exact text
    assert hashlib.sha256(body.encode()).hexdigest() == (
        "be0a077edeb0da531db26e4a3186d8c529c80b44c80070146218f34b12e428d5"
    )


def test_only_built_items_have_entries() -> None:
    """Every Old Maker item in the canon is built: the Tricorder, the Power Pack and its
    two uses (keeping your things and transport, each a per-world switch), the No-Spawn
    Beacon, the Replicator and the Wormhole Seed (§M12), so each has its entry."""
    assert set(guide.ENTRIES) == {
        "intro_game",
        "intro_owner",
        "tricorder",
        "power_pack",
        "power_pack_keep",
        "power_pack_transport",
        "power_pack_unused",
        "no_spawn_beacon",
        "replicator",
        "wormhole_seed",
    }
    # The canon's recipe and rules, as the lore page says them.
    assert "eight Ender Pearls in a ring around one Blaze Powder" in guide.ENTRIES["power_pack"]
    # Bound to its owner, so the "someone else's" static is a safety net Dave never explains.
    assert "can't be lost" in guide.ENTRIES["tricorder"]
    assert "static" not in guide.ENTRIES["tricorder"]
    assert "their own Tricorder" in guide.ENTRIES["power_pack_keep"]
    beacon = guide.ENTRIES["no_spawn_beacon"]
    assert "a Power Pack in the middle of a crafting table, surrounded by eight echo" in beacon
    assert "so does the Warden" in beacon and "the Ender Dragon and the Wither" in beacon
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


def test_dave_tells_the_story_canon_and_keeps_the_mystery_sealed() -> None:
    """The lore page's canon (2026-10-12) rides in Dave's persona, in game and in the app:
    the ruins and the tie-ins, and why the Old Makers vanished stays sealed."""
    from jbrain.agent.agents import AGENTS

    persona = " ".join(AGENTS["minecraft_dave"].prompt.split())
    assert "That part of my memory is sealed, crew. Maybe one day." in persona
    assert "Ancient cities were their deepest archive" in persona
    assert "Copper golems are their little helpers" in persona
    # Proposals stay out until the owner promotes them.
    assert "copper castle" not in persona
    assert "Memory crystals" not in persona
