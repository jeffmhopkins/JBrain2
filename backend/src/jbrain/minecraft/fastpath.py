"""Dave's fast path: simple asks answered without the model (MINECRAFT_BEDROCK_PLAN.md §M18).

Most of what players say to Dave in game is a handful of fixed requests. A `/jb:dave`
question that CLEARLY is one of them is carried out at once, through the very tool
handlers a model turn would call (so the asker-only rule, the deep-scan limit and every
other check are theirs, not re-implemented here), and answered with Dave's signed-off
quick-answer lines. The door (`minecraft/door.py`) writes the exchange into the player's
session like any other, so a later model turn and the PWA both see it.

Strict by design: a wrong fast answer is worse than a slow right one.
- A question matches only as a whole: anything more in it ("navigate to tree farm but
  avoid the river") isn't a match and goes to the model.
- An outcome with no signed-off line (a place in another dimension, a write the tool
  refused, a search that found nothing) also goes to the model. That is safe because
  nothing the fast path does is harmed by doing it again: pointing the Tricorder, saving
  a place where they stand, and reading are idempotent, a refused write wrote nothing,
  and a deep scan already paid for today is a free repeat.
- No line here names an Old Maker item other than the Tricorder, which every player has
  (§M19: a locked item is never named).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jbrain.agent.agents import MINECRAFT_DAVE_INGAME_TOOLS
from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.agent.minecrafttools import Found, bearing, first_hit, place_name
from jbrain.minecraft import scans
from jbrain.minecraft.asker import Asker, Spot
from jbrain.minecraft.sessions import check_gamertag

# --- Dave's quick answers -----------------------------------------------------------------
# Copied from the canon: Bleepbloop_City `notes/lore.md`, "Dave's lines" → "Quick
# answers", signed off by the owner (2026-10-11). The lore page is the source: a line
# changes there first, then here. Only the italic gaps are filled in.

_DIMENSION = {"overworld": "Overworld", "nether": "Nether", "the_end": "End"}


def locked_on(place: str, blocks: int, way: str) -> str:
    return f"Locked on: {place}, {blocks} blocks {way}. Hold your Tricorder and follow the arrow."


def no_such_place(place: str) -> str:
    return (
        f"I don't have a place called {place} for you. Stand there and say"
        f' "save this place as {place}".'
    )


ARROW_CLEARED = "Arrow cleared. Ask me when you need a heading."


def marked(place: str) -> str:
    return f"Marked: {place}, right where you're standing."


def forgotten(place: str) -> str:
    return f"{place[:1].upper()}{place[1:]} is gone from my list."


def your_places(places: list[str]) -> str:
    return f"Your places: {', '.join(places)}."


def from_up_here(at: Spot, biome: str | None) -> str:
    where = f"{at.x}, {at.y}, {at.z}"
    land = f" in {_biome_phrase(biome)}," if biome else ""
    return f"From up here: {where},{land} in the {_DIMENSION[at.dim]}."


def they_are(gamertag: str, blocks: int, way: str, dim: str) -> str:
    return f"{gamertag} is {blocks} blocks {way} of you, in the {_DIMENSION[dim]}."


def last_saw(gamertag: str, at: Spot) -> str:
    return f"{gamertag} isn't online; I last saw them near {at.x}, {at.y}, {at.z}."


def scans_left(n: int) -> str:
    return f"You've got {_scans(n)} left today. Sleep through the night and I'll have two again."


# The everyday answer the lore gives for "where's the nearest village?" (signed off with
# the quick answers as an example of Dave's light touch), with the scan count M17 says.
def found_one(blocks: int, way: str, at: Spot, arrow: bool, left: int | None) -> str:
    coords = f"{at.x}, {at.z}" if at.y is None else f"{at.x}, {at.y}, {at.z}"
    out = f"From up here I see one {blocks} blocks {way}, at {coords}."
    if arrow:
        out += " Hold your Tricorder and follow the arrow, crew."
    if left is not None:
        out += f" You've got {_scans(left)} left today."
    return out


def _scans(n: int) -> str:
    return "no deep scans" if n <= 0 else f"{n} deep scan{'' if n == 1 else 's'}"


def _biome_phrase(biome: str) -> str:
    name = biome.lower()
    if name.startswith("the "):
        return name
    # "Plains", "Badlands", "Dripstone Caves": a place, not one of a kind.
    if name.endswith("s"):
        return f"the {name}"
    return f"{'an' if name[:1] in 'aeiou' else 'a'} {name}"


# --- what matches ---------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """One recognised request. `name` is a place or a gamertag as typed; `kind` and `what`
    say what a nearest-search looks for."""

    action: str
    name: str = ""
    kind: str = ""
    what: str = ""
    label: str = ""


# What "nearest" may name, in the player's words → the Bedrock id. Structures are the
# ones `locate` finds in the Overworld (deploy/minecraft/mapping.py STRUCTURES, each
# checked on a 1.26 server); blocks are ores and spawners `mc_find_block` reads.
STRUCTURES = {
    "village": "village",
    "mansion": "mansion",
    "woodland mansion": "mansion",
    "monument": "monument",
    "ocean monument": "monument",
    "outpost": "pillager_outpost",
    "pillager outpost": "pillager_outpost",
    "ancient city": "ancient_city",
    "trail ruins": "trail_ruins",
    "trial chamber": "trial_chambers",
    "trial chambers": "trial_chambers",
    "stronghold": "stronghold",
    "temple": "temple",
    "ocean ruins": "ruins",
    "shipwreck": "shipwreck",
    "buried treasure": "buried_treasure",
    "ruined portal": "ruined_portal",
    "mineshaft": "mineshaft",
}
BLOCKS = {
    "diamond": "diamond_ore",
    "diamonds": "diamond_ore",
    "diamond ore": "diamond_ore",
    "iron": "iron_ore",
    "iron ore": "iron_ore",
    "gold": "gold_ore",
    "gold ore": "gold_ore",
    "coal": "coal_ore",
    "coal ore": "coal_ore",
    "copper": "copper_ore",
    "copper ore": "copper_ore",
    "emerald": "emerald_ore",
    "emeralds": "emerald_ore",
    "emerald ore": "emerald_ore",
    "redstone": "redstone_ore",
    "redstone ore": "redstone_ore",
    "lapis": "lapis_ore",
    "lapis ore": "lapis_ore",
    "lapis lazuli": "lapis_ore",
    "ancient debris": "ancient_debris",
    "spawner": "mob_spawner",
    "mob spawner": "mob_spawner",
}

# A name the fast path will act on: a few plain words. Anything carrying an instruction
# or a reference ("but", "my", "nearest", "it") is the model's to read.
_NAME_CHARS = re.compile(r"^[a-z0-9][a-z0-9' -]*$", re.IGNORECASE)
_NAME_WORDS_MAX = 4
_NOT_A_NAME = frozenset(
    (  # noqa: SIM905 — a word list reads better as words
        "a an and but or then so if when while because avoid avoiding without via through"
        " past around near nearest closest please it that this there here me my you your"
        " where what which to from"
    ).split()
)
_GAME_ID_WORDS = frozenset({"the", "a"})

_P = r"(?:please )?"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"^{_P}(?:point me|navigate|guide me) home$"), "home"),
    (
        re.compile(rf"^{_P}(?:point me|navigate|navigate me|guide me) to (?P<name>.+)$"),
        "waypoint",
    ),
    (re.compile(rf"^{_P}clear (?:my |the )?(?:waypoint|arrow)$"), "clear"),
    (re.compile(rf"^{_P}(?:save|mark) this (?:place|location|spot) as (?P<name>.+)$"), "save"),
    (re.compile(rf"^{_P}forget (?:my place |the place |place )?(?P<name>.+)$"), "forget"),
    (re.compile(rf"^{_P}(?:remove|delete) (?:my |the )?(?:saved )?place (?P<name>.+)$"), "forget"),
    (re.compile(rf"^{_P}(?:list|show me|show|what are) my (?:saved )?places$"), "places"),
    (re.compile(r"^where am i(?: now| right now)?$"), "where_am_i"),
    (
        re.compile(
            r"^how many (?:deep )?scans (?:do i have left|have i got left|do i have|are left"
            r"|left)(?: today)?$"
        ),
        "scans",
    ),
    (
        re.compile(
            rf"^{_P}(?:(?:where is|where's|wheres|find me|find) )?(?:the |a )?"
            r"(?:nearest|closest) (?P<thing>.+)$"
        ),
        "nearest",
    ),
    (re.compile(r"^(?:where is|where's|wheres) (?P<name>.+)$"), "where_is"),
]


def _plain(text: str) -> str:
    """Spacing collapsed and the end's punctuation and "please" dropped; case kept, so a
    place is saved as it was typed."""
    out = " ".join(text.split()).strip(" ?!.")
    if out.lower().endswith(" please"):
        out = out[: -len(" please")].rstrip(" ,?!.")
    return out


def _place(raw: str) -> str:
    name = place_name(raw)
    words = name.lower().split()
    if (
        not name
        or not _NAME_CHARS.match(name)
        or len(words) > _NAME_WORDS_MAX
        or _NOT_A_NAME & set(words)
        or sum(w.lstrip("-").isdigit() for w in words) > 1  # coordinates, not a name
    ):
        return ""
    return name


def match(text: str) -> Ask | None:
    """The request this question clearly is, or None for the model."""
    plain = _plain(text)
    lowered = plain.lower()
    for pattern, action in _PATTERNS:
        m = pattern.match(lowered)
        if m is None:
            continue
        # The name as typed: the same span of the original, which differs only in case.
        name = plain[m.start("name") : m.end("name")] if "name" in pattern.groupindex else ""
        if action == "home":
            return Ask("waypoint", name="home")
        if action in ("waypoint", "save", "forget"):
            place = _place(name)
            return Ask(action, name=place) if place else None
        if action == "nearest":
            thing = m.group("thing")
            if thing in STRUCTURES:
                return Ask("nearest", kind="structure", what=STRUCTURES[thing], label=thing)
            if thing in BLOCKS:
                return Ask("nearest", kind="block", what=BLOCKS[thing], label=thing)
            return None
        if action == "where_is":
            tag = check_gamertag(name)
            if tag is None or tag.split()[0].lower() in _NOT_A_NAME | _GAME_ID_WORDS:
                return None
            return Ask("where_is", name=tag)
        return Ask(action)
    return None


# --- carrying it out ------------------------------------------------------------------


class Unanswered(Exception):
    """The fast path can't give a signed-off answer; the question goes to the model."""


@dataclass(frozen=True)
class Answer:
    reply: str
    steps: list[dict[str, Any]]


_LEFT = re.compile(r"\b(\d+) scans? left today\b")


class FastPath:
    """Runs a matched request through the in-game tool handlers. The asker must already
    be registered for the session (`asker.asking`), exactly as for a model turn."""

    def __init__(self, tools: Mapping[str, ToolHandler]) -> None:
        self._tools = tools

    async def answer(self, ask: Ask, asker: Asker, ctx: ToolContext) -> Answer | None:
        steps: list[dict[str, Any]] = []
        try:
            reply = await self._carry_out(ask, asker, ctx, steps)
        except Unanswered:
            return None
        return Answer(reply, steps)

    async def _call(
        self, name: str, args: dict[str, Any], ctx: ToolContext, steps: list[dict[str, Any]]
    ) -> str:
        # Never a tool a player's model turn couldn't hold.
        handler = self._tools.get(name) if name in MINECRAFT_DAVE_INGAME_TOOLS else None
        if handler is None:
            raise Unanswered(name)
        out = await handler(args, ctx)
        brief = str(getattr(out, "result_brief", ""))
        steps.append(
            {
                "id": f"fastpath-{len(steps) + 1}",
                "name": name,
                "ok": True,
                "sources": [],
                "text_offset": 0,
                "reasoning_offset": 0,
                **({"args": args} if args else {}),
                "summary": str(out),
                **({"result_brief": brief} if brief else {}),
            }
        )
        return out

    async def _carry_out(
        self, ask: Ask, asker: Asker, ctx: ToolContext, steps: list[dict[str, Any]]
    ) -> str:
        def facts(out: str) -> dict[str, Any]:
            if not isinstance(out, Found):
                raise Unanswered(ask.action)
            return out.found

        def brief(out: str) -> str:
            return str(getattr(out, "result_brief", ""))

        here = asker.at
        if ask.action == "waypoint":
            out = await self._call("mc_set_waypoint", {"place": ask.name}, ctx, steps)
            if brief(out) == "no such place":
                return no_such_place(ask.name)
            target: Spot = facts(out)["target"]
            if target.dim != here.dim:
                raise Unanswered("another dimension")
            way, far = bearing(here.x, here.z, target.x, target.z)
            label = "home" if ask.name.casefold() == "home" else brief(out)
            return locked_on(label, far, way)
        if ask.action == "clear":
            out = await self._call("mc_set_waypoint", {"clear": True}, ctx, steps)
            if brief(out) != "cleared":
                raise Unanswered("not cleared")
            return ARROW_CLEARED
        if ask.action == "save":
            out = await self._call("mc_place_save", {"name": ask.name}, ctx, steps)
            if brief(out) not in (ask.name, f"{ask.name} (moved)"):
                raise Unanswered("not saved")
            return marked(ask.name)
        if ask.action == "forget":
            out = await self._call("mc_place_remove", {"name": ask.name}, ctx, steps)
            if not brief(out).startswith("removed "):
                raise Unanswered("not removed")
            return forgotten(brief(out).removeprefix("removed "))
        if ask.action == "places":
            out = await self._call("mc_places", {}, ctx, steps)
            shown = []
            for name, at in facts(out)["places"]:
                if at.dim != here.dim:
                    shown.append(f"{name} (in the {_DIMENSION[at.dim]})")
                    continue
                way, far = bearing(here.x, here.z, at.x, at.z)
                shown.append(f"{name} ({far} blocks {way})")
            if not shown:
                raise Unanswered("no places")
            return your_places(shown)
        if ask.action == "where_am_i" or (
            ask.action == "where_is" and ask.name.casefold() == asker.gamertag.casefold()
        ):
            out = await self._call("mc_what_is_at", {}, ctx, steps)
            biome = out.found.get("biome") if isinstance(out, Found) else None
            return from_up_here(here, biome)
        if ask.action == "where_is":
            out = await self._call("mc_players", {"gamertag": ask.name}, ctx, steps)
            seen = facts(out)
            spot: Spot | None = seen["spot"]
            if spot is None:
                raise Unanswered("never seen here")
            if not seen["online"]:
                return last_saw(seen["gamertag"], spot)
            if spot.dim != here.dim:
                raise Unanswered("another dimension")
            way, far = bearing(here.x, here.z, spot.x, spot.z)
            if far < 3:
                raise Unanswered("right beside them")
            return they_are(seen["gamertag"], far, way, spot.dim)
        if ask.action == "scans":
            out = await self._call("mc_world_info", {}, ctx, steps)
            return scans_left(int(facts(out)["scans_left"]))
        if ask.action == "nearest":
            return await self._nearest(ask, asker, ctx, steps)
        raise Unanswered(ask.action)

    async def _nearest(
        self, ask: Ask, asker: Asker, ctx: ToolContext, steps: list[dict[str, Any]]
    ) -> str:
        here = asker.at
        if ask.kind == "structure":
            # `locate` searches the Overworld only; from elsewhere the model explains.
            if here.dim != "overworld":
                raise Unanswered("locate is Overworld only")
            out = await self._call("mc_locate", {"kind": "structure", "id": ask.what}, ctx, steps)
        else:
            out = await self._call("mc_find_block", {"blocks": [ask.what]}, ctx, steps)
        if str(getattr(out, "result_brief", "")) == "out of scans":
            return scans.RECHARGE
        hit = first_hit(out, here.dim)
        if hit is None:
            raise Unanswered("nothing found")
        point = await self._call(
            "mc_set_waypoint",
            {
                "x": hit.x,
                "y": hit.y,
                "z": hit.z,
                "dimension": hit.dim,
                "label": ask.label[:1].upper() + ask.label[1:],
            },
            ctx,
            steps,
        )
        left = _LEFT.search(out)
        way, far = bearing(here.x, here.z, hit.x, hit.z)
        return found_one(
            far, way, hit, isinstance(point, Found), int(left.group(1)) if left else None
        )
