"""Dave's item guide, unlocked as each item becomes craftable (MINECRAFT_BEDROCK_PLAN.md §M19).

Canon (Bleepbloop_City `notes/lore.md`, "Dave explains each item only once you can make
it"): Dave explains an Old Maker item to a player only once they could craft it, and
before that doesn't mention it at all. That is enforced by what reaches the model, not by
asking it to keep a secret: an in-game turn's system prompt carries ONLY the entries the
asker has unlocked, so a locked item is not in its context to leak.

- **The entries** are prompt material (`agent/prompts/minecraft_dave_guide.prompt`), one
  per built item. An item whose wave hasn't shipped has no entry at all.
- **The unlocks** are the pack's, per player per world (`jbrain-pack/scripts/rules.js`
  `unlocksFrom`), sent with each `/jb:dave` question as `known`.
- **The owner's PWA chats** get every built entry.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jbrain.llm.promptfile import load_prompt

_PROMPT = load_prompt(
    Path(__file__).resolve().parents[1] / "agent" / "prompts" / "minecraft_dave_guide.prompt"
)
VERSION = _PROMPT.version
_ENTRY = re.compile(r"^\[entry: (\w+)\]\n", re.MULTILINE)

# What the pack can report a player has unlocked: the Tricorder from the start, the Power
# Pack on their first Ender Pearl, its uses on their first Power Pack, the Replicator once
# they hold 8 Power Packs (§M15).
UNLOCKS = ("tricorder", "power_pack", "power_pack_uses", "replicator")
ALWAYS = frozenset({"tricorder"})


def _entries(body: str) -> dict[str, str]:
    parts = _ENTRY.split(body)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


ENTRIES = _entries(_PROMPT.body)


def parse_known(raw: Any) -> frozenset[str]:
    """A question's `known` list as the unlocks it may name; anything else is ignored.
    An older pack sends none, which is the Tricorder alone: the fail-safe direction."""
    names = raw if isinstance(raw, list) else []
    return ALWAYS | {n for n in names if isinstance(n, str) and n in UNLOCKS}


def entries_for(known: Iterable[str], *, keep_switch: bool) -> list[str]:
    """The entry names a player with these unlocks may see, in reading order. Keeping your
    things is a use only a world with its switch on has; elsewhere a made pack does
    nothing, and the guide says so rather than describing a use the world lacks."""
    have = set(known) | ALWAYS
    out = ["tricorder"]
    if have & {"power_pack", "power_pack_uses"}:
        out.append("power_pack")
    if "power_pack_uses" in have:
        out.append("power_pack_keep" if keep_switch else "power_pack_unused")
    if "replicator" in have:
        out.append("replicator")
    return out


def for_player(known: Iterable[str], *, keep_switch: bool) -> str:
    names = ["intro_game", *entries_for(known, keep_switch=keep_switch)]
    return "\n\n".join(ENTRIES[n] for n in names)


def for_owner() -> str:
    names = ["intro_owner", "tricorder", "power_pack", "power_pack_keep", "replicator"]
    return "\n\n".join(ENTRIES[n] for n in names)
