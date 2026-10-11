"""Minecraft_Dave's wormhole tool: `mc_wormhole_aim` (docs/plans/MINECRAFT_BEDROCK_PLAN.md
§M12; canon: Bleepbloop_City `notes/lore.md`, "Wormhole gates").

A player plants a Wormhole Seed in a ring of diamond blocks, and Dave opens the gate to
one of that player's OWN saved places, or re-aims it at another. The checks are split by
who can make them:

- **here**, from the database: the place is one of the player's own saved places in the
  loaded world (`app.mc_places`, the transport rule), and its dimension is one they have
  been to (the travel log, `app.mc_player_track`);
- **in the pack**, from the live world (`jbrain-pack/scripts/wormhole.js`): the player is
  online and within 4 blocks of a gate they planted, the ring is clear, and a re-aim's
  Power Pack is in their inventory. The pack takes the pack only once the gate changed.

The request is a `scriptevent jb:wormhole` the pack carries out and answers on the console,
read back the way `mc_set_waypoint` reads its own. No gamertag is taken from the model: in
game the gate is the asker's, in the app it is the chat's player's.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import text

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.minecrafttools import (
    _DIMENSION_NAMES,
    PLACE_NAME_MAX,
    Player,
    _guarded,
    _places,
    _reply,
    _spot,
    chat_player_for,
    fence,
    place_name,
    spot_text,
)
from jbrain.db.session import scoped_session
from jbrain.minecraft import client as mc
from jbrain.minecraft.asker import Spot, asker_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.settings_store import SqlSettingsStore

_LEFT = re.compile(r"1 Power Pack used, (\d+) left")
_GATE = re.compile(r"gate (\d+)")

NO_PLAYER = (
    "This chat has no Minecraft player yet, so no gate was changed. Ask which player it's"
    " for and call mc_player with their gamertag."
)


def aim_event(gamertag: str, place: str, spot: Spot) -> str:
    """The console command. json.dumps escapes every quote and control character, so a
    place's name can't end the command or start another."""
    event = {
        "op": "aim",
        "player": gamertag,
        "place": place,
        "x": spot.x,
        "y": spot.y,
        "z": spot.z,
        "dim": spot.dim,
    }
    return f"scriptevent jb:wormhole {json.dumps(event)}"


def answer(reply: str, gamertag: str, place: str, spot: Spot) -> ToolOutput:
    """The pack's console answer as the model's result: what changed, what it cost, or
    why nothing did. Its reasons name players, so they reach the model fenced."""
    gate = m.group(1) if (m := _GATE.search(reply)) else "?"
    there = f"{fence(place)} at {spot_text(spot)}"
    one_way = (
        " It's one-way: anyone who steps in comes out there, and nothing comes back through it."
    )
    if "wormhole opened" in reply:
        return ToolOutput(
            f"Opened {fence(gamertag)}'s gate {gate} to {there}. The first opening is"
            f" free.{one_way}",
            result_brief=f"opened → {place}",
        )
    if "wormhole aimed" in reply:
        left = m.group(1) if (m := _LEFT.search(reply)) else "?"
        return ToolOutput(
            f"Re-aimed {fence(gamertag)}'s gate {gate} at {there}. That used one Power"
            f" Pack; {left} left.{one_way}",
            result_brief=f"re-aimed → {place}",
        )
    if "wormhole unchanged" in reply:
        return ToolOutput(
            f"{fence(gamertag)}'s gate {gate} already leads to {there}. Nothing changed and"
            " no Power Pack was used.",
            result_brief="already there",
        )
    if "wormhole refused" in reply:
        why = reply.split("wormhole refused:", 1)[1].strip()
        return ToolOutput(
            f"The gate wasn't changed and nothing was used: {fence(why)}. Tell them plainly;"
            " a gate is opened by the player who planted it, standing within 4 blocks of it,"
            " and each re-aim costs one Power Pack from their inventory.",
            result_brief="refused",
        )
    return ToolOutput(
        "The server took the command, but the jbrain pack didn't answer, so the gate may"
        " not have changed — the box's own jbrain pack may not have loaded in this world"
        " (check the server log for its `[jbrain]` lines).",
        result_brief="no answer",
    )


async def _visited(s: AsyncSession, key: str, world: str, dim: str) -> bool:
    row = await s.execute(
        text(
            "SELECT 1 FROM app.mc_player_track WHERE xuid = :k AND world = :w AND dim = :d LIMIT 1"
        ),
        {"k": key, "w": world, "d": dim},
    )
    return row.first() is not None


def build_minecraft_wormhole_handlers(
    maker: async_sessionmaker[AsyncSession],
    config: Any,
    settings_store: SqlSettingsStore | None,
) -> dict[str, ToolHandler]:
    """`mc_wormhole_aim`, bound like the other Minecraft tools: the sidecar's console for
    the pack, and the owner-RLS'd saved places and travel log under `ctx.session`."""

    async def sidecar(method: str, path: str, **kw: Any) -> dict[str, Any] | str:
        try:
            return await mc.call(config, method, path, **kw)
        except HTTPException as exc:
            return f"The Minecraft server can't answer right now: {exc.detail}"

    async def mc_wormhole_aim(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        name = place_name(arguments.get("place"))
        if not name:
            return (
                f"place is one of the player's saved places (mc_places lists them),"
                f" 1–{PLACE_NAME_MAX} characters."
            )
        asker = asker_for(ctx.agent_session_id)
        if asker is not None:
            world = asker.world
        else:
            worlds = await sidecar("GET", "/worlds")
            world = str(worlds.get("active") or "") if isinstance(worlds, dict) else ""
        if not world:
            return "The Minecraft server can't say which world is loaded, so no gate was changed."
        async with scoped_session(maker, ctx.session) as s:
            player = (
                Player(asker.key, asker.gamertag)
                if asker is not None
                else await chat_player_for(s, settings_store, ctx.session, ctx.agent_session_id)
            )
            if player is None:
                return ToolOutput(NO_PLAYER, result_brief="no player")
            rows = await _places(s, player.key, world)
            row = next((r for r in rows if r.name.casefold() == name.casefold()), None)
            if row is None:
                known = ", ".join(r.name for r in rows)
                return ToolOutput(
                    f"{fence(player.gamertag)} has no saved place called {fence(name)}, so"
                    " no gate was changed. A gate only opens to one of their own saved"
                    " places: they can stand there and ask you to save it (mc_place_save)."
                    + (f" Their saved places: {fence(known)}." if known else ""),
                    result_brief="no such place",
                )
            spot = _spot(row)
            visited = (
                spot.dim == "overworld"
                or (asker is not None and asker.at.dim == spot.dim)
                or await _visited(s, player.key, world, spot.dim)
            )
        if spot.y is None:
            return ToolOutput(
                f"{fence(row.name)} was saved without a height, so a gate can't land there."
                " Save it again standing on the spot.",
                result_brief="no height",
            )
        if not visited:
            return ToolOutput(
                f"{fence(player.gamertag)} hasn't been to the {_DIMENSION_NAMES[spot.dim]}"
                " yet, so no gate can open there. Nothing changed.",
                result_brief="not visited",
            )
        got = await sidecar(
            "POST",
            "/command",
            json={"command": aim_event(player.gamertag, str(row.name), spot), "wait_s": 3.0},
        )
        if isinstance(got, str):
            return got
        reply = _reply([str(line) for line in got.get("lines", [])])
        return answer(reply, player.gamertag, str(row.name), spot)

    return {"mc_wormhole_aim": _guarded("mc_wormhole_aim", mc_wormhole_aim)}
