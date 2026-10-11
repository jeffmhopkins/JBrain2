"""Minecraft_Dave's one acting tool: `mc_transport`, transport on request
(docs/plans/MINECRAFT_BEDROCK_PLAN.md §M10, "Teleport to a known place, one Power Pack per
trip"; canon: Bleepbloop_City `notes/lore.md`, "Transport on request").

Only in game, only the asker, only to one of the asker's own saved places
(`app.mc_places`, which only ever records where they stood). The backend never moves
anyone: it looks the place up and hands it to the jbrain pack as
`scriptevent jb:transport {…}`, and the pack does the rest in the world: their own
Tricorder and a Power Pack, the world's "Power Pack teleport" switch, a dimension they've
been in, then a Yes on a server form, a clear landing, and one pack burnt only if the
teleport lands. Each step is one console line, `transport <id> <outcome> {json}`; the tool
reads the offer back at once, then watches the log for the answer, which the pack gives
up on after 45 s so a late Yes can't move anyone this tool stopped watching for.

A trip that was tried (moved, or refused at the landing) is written to the player's
progress log as an `auto` entry: who, from, to, and the pack spent or kept.

Canon (§M19): an Old Maker item is named only to a player who has unlocked it. Transport
is a use of the Power Pack, so a player who has never held one gets a refusal that names
neither.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import text

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.minecrafttools import (
    _DIMENSION_NAMES,
    PLACE_NAME_MAX,
    _guarded,
    _places,
    _spot,
    fence,
    place_name,
    spot_text,
)
from jbrain.db.session import scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft.asker import Asker, Spot, asker_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# The pack holds an offer 45 s (transport_rules.js OFFER_TTL_MS); this waits past it, so
# the last answer it can give is always seen.
WAIT_S = 50.0
POLL_S = 1.0
LOG_TAIL = 400
UNLOCK = "power_pack_uses"

_LINE = re.compile(r"\btransport ([0-9a-f]{8}) ([a-z-]+) (\{.*\})\s*$")

_REFUSED = {
    "off": "Teleporting isn't allowed in this world (Jeff's switch). Nothing was spent.",
    "no-tricorder": "They need their own Tricorder on them for the transporter to lock on.",
    "no-pack": (
        "They have no Power Pack on them, and each trip burns one (eight Ender Pearls around"
        " a Blaze Powder). Nothing was spent. Offer to point their Tricorder there instead"
        " (mc_set_waypoint with the place)."
    ),
    "unexplored": (
        "That place has no safe height on record, so it can't be a landing; their Tricorder"
        " can still point there (mc_set_waypoint)."
    ),
    "unvisited": (
        "That place is in a dimension they haven't been to yet, and the transporter only goes"
        " where they've been. Nothing was spent."
    ),
    "offline": "They're not in the world any more, so nothing happened.",
    "busy": "A transport question is already open on their screen; they can answer that one.",
}

_ENDED = {
    "declined": "They chose No on the form. Nothing was spent and they stayed put.",
    "expired": "They didn't answer the form in time. Nothing was spent; they can ask again.",
    "blocked": (
        "The landing at {label} is blocked: something's been built over the spot, or there"
        " isn't room to stand. Nothing was spent. They can clear it, or stand somewhere open"
        " there and save the place again."
    ),
    "unloaded": (
        "The ground at {label} couldn't be reached just now. Nothing was spent; they can try again."
    ),
    "offline": "They left the world before the trip. Nothing was spent.",
    "failed": "The transport failed in the game. Nothing was spent. Say so; don't guess why.",
}


def parse_line(line: str, trip: str) -> tuple[str, dict[str, Any]] | None:
    """One trip's console line → (outcome, detail); None for any other line."""
    m = _LINE.search(line)
    if m is None or m.group(1) != trip:
        return None
    try:
        detail = json.loads(m.group(3))
    except ValueError:
        return None
    return m.group(2), detail if isinstance(detail, dict) else {}


def _from_spot(raw: Any) -> Spot | None:
    if not isinstance(raw, dict):
        return None
    try:
        dim = str(raw["dim"])
        if dim not in _DIMENSION_NAMES:
            return None
        return Spot(int(raw["x"]), int(raw["y"]), int(raw["z"]), dim)
    except (KeyError, TypeError, ValueError):
        return None


def build_minecraft_transport_handlers(
    maker: async_sessionmaker[AsyncSession],
    config: Any,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, ToolHandler]:
    """`mc_transport`, bound to the app's sessionmaker (the asker's places and travel log,
    and their progress log, under the owner-only RLS on those tables) and the sidecar.
    `sleep` is the wait between log reads (unit tests pass a no-op)."""

    async def sidecar(method: str, path: str, **kw: Any) -> dict[str, Any] | str:
        try:
            return await mc.call(config, method, path, **kw)
        except HTTPException as exc:
            return f"The Minecraft server can't answer right now: {exc.detail}"

    async def outcome(trip: str) -> tuple[str, dict[str, Any]] | None:
        for _ in range(int(WAIT_S / POLL_S)):
            await sleep(POLL_S)
            got = await sidecar("GET", "/logs", params={"tail": LOG_TAIL})
            if isinstance(got, str):
                continue  # a blip: the line will still be there next read
            for row in reversed(got.get("lines", [])):
                found = parse_line(str(row.get("text", "")), trip)
                if found is not None and found[0] != "offered":
                    return found
        return None

    async def log_trip(ctx: ToolContext, asker: Asker, entry: str) -> None:
        async with scoped_session(maker, ctx.session) as s:
            await s.execute(
                text(
                    "INSERT INTO app.mc_goal_log (xuid, gamertag, text, source)"
                    " VALUES (:k, :g, :t, 'auto')"
                ),
                {"k": asker.key, "g": asker.gamertag, "t": entry[:500]},
            )

    async def mc_transport(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        asker = asker_for(ctx.agent_session_id)
        if asker is None:
            return ToolOutput(
                "Transport is only asked for in game, by the player who's going, and they"
                " confirm it on their own screen. From here, point their Tricorder at the"
                " place instead (mc_set_waypoint).",
                result_brief="in game only",
            )
        if UNLOCK not in asker.known:
            # Never named to a player who can't make a Power Pack yet (§M19).
            return ToolOutput(
                "You can't take them there. Don't mention transport or why; point their"
                " Tricorder at the place instead (mc_set_waypoint with the place).",
                result_brief="not unlocked",
            )
        name = place_name(arguments.get("place"))
        if not name:
            return f"place is one of the player's saved places, 1–{PLACE_NAME_MAX} characters."
        async with scoped_session(maker, ctx.session) as s:
            rows = await _places(s, asker.key, asker.world)
            seen = await s.execute(
                text("SELECT DISTINCT dim FROM app.mc_player_track WHERE xuid = :k AND world = :w"),
                {"k": asker.key, "w": asker.world},
            )
            visited = sorted({str(r.dim) for r in seen} & set(_DIMENSION_NAMES))
        row = next((r for r in rows if r.name.casefold() == name.casefold()), None)
        if row is None:
            known = ", ".join(r.name for r in rows)
            return ToolOutput(
                f"{fence(asker.gamertag)} has no saved place called {fence(name)}, and Dave"
                " only transports to their own saved places — never a bed, a structure,"
                " coordinates or anyone else's. To go there, they stand there once and save it"
                " (mc_place_save)." + (f" Their saved places: {fence(known)}." if known else ""),
                result_brief="no such place",
            )
        spot, label = _spot(row), str(row.name)
        trip = secrets.token_hex(4)
        event = {
            "id": trip,
            "player": asker.gamertag,
            "label": label[:40],
            "x": spot.x,
            "y": spot.y,
            "z": spot.z,
            "dim": spot.dim,
            "visited": visited,
        }
        # json.dumps escapes every quote and control character, so the label can't end the
        # command or start another; the console takes it as one line.
        got = await sidecar(
            "POST",
            "/command",
            json={"command": f"scriptevent jb:transport {json.dumps(event)}", "wait_s": 3.0},
        )
        if isinstance(got, str):
            return got
        first = next(
            (f for f in (parse_line(str(ln), trip) for ln in got.get("lines", [])) if f), None
        )
        if first is None:
            return (
                "The server took the command, but the jbrain pack didn't answer, so nothing"
                " happened. Its transport may not be loaded in this world."
            )
        state, detail = first
        if state == "refused":
            why = str(detail.get("why", ""))
            return ToolOutput(
                _REFUSED.get(why, "The transporter refused. Nothing was spent."),
                result_brief=f"refused: {why or '?'}",
            )
        ended = await outcome(trip)
        if ended is None:
            return ToolOutput(
                "No answer came back from the form. Nothing should have been spent; tell them"
                " to ask again if they still want to go.",
                result_brief="no answer",
            )
        state, detail = ended
        there = f"{label} {spot_text(spot)}"
        if state == "moved":
            left = detail.get("left")
            came = _from_spot(detail.get("from"))
            await log_trip(
                ctx,
                asker,
                f"Transported{f' from {spot_text(came)}' if came else ''} to {there};"
                f" 1 Power Pack spent, {left} left.",
            )
            return ToolOutput(
                f"Done: {fence(asker.gamertag)} is at {fence(label)} {spot_text(spot)}. One"
                f" Power Pack burnt; {left} left.",
                result_brief=f"to {label}",
            )
        if state == "refused":
            why = str(detail.get("why", ""))
            return ToolOutput(
                _REFUSED.get(why, "The transporter refused. Nothing was spent."),
                result_brief=f"refused: {why or '?'}",
            )
        if state in ("blocked", "unloaded"):
            await log_trip(
                ctx, asker, f"Transport to {there} stopped at the landing ({state}); pack kept."
            )
        return ToolOutput(
            _ENDED.get(state, _ENDED["failed"]).format(label=fence(label)),
            result_brief=state,
        )

    return {"mc_transport": _guarded("mc_transport", mc_transport)}
