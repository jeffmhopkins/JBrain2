"""Players' goals and their progress, for the owner's Minecraft screen (plan §M24, the
GUI gate `docs/mocks/minecraft-goals/`, variant B, look-only).

Two reads. `read_goals` is the database alone: every player's goals and the latest of
their journal (`app.mc_goal_log`). `count_progress` is §M24's **app path** of
`mc_goal_progress`, batched per player: one `jb:carry` and one `/map/stock` per dimension
(per eight items) for all of a player's counted open goals in a world. It counts exactly
what Dave counts in game — what the player carries, plus the containers within
NEAR_BLOCKS of each of their saved places **and of where they stand**, each container
once toward its nearest centre — but never charges a deep scan: `scans.charge` is the
in-game asker's, and the app has none. A place more than NEAR_BLOCKS from where the
player stands (or in another dimension) is counted here like any other and only marked
`far`, because in game that check costs one deep scan for all far places together.

A goal's world is the world folder it was made in (places are filed the same way); one
made before goals recorded it (NULL) is the loaded world's. A world that isn't loaded is
still counted, from its saved files, which are exact while nobody plays it; only the
pockets need the world running, and the player in it.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from jbrain.agent.minecrafttools import NEAR_BLOCKS, Pocket, carry_pocket, full_id, stock_near
from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import sessions
from jbrain.minecraft.asker import Spot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_OWNER = SessionContext(principal_kind="owner")
Sidecar = Callable[..., Awaitable[dict[str, Any] | str]]
# Per goal, per player: enough for "what's been happening" without paging a diary.
JOURNAL_PER_GOAL = 10


class Unavailable(Exception):
    """The sidecar couldn't say which worlds there are, so nothing can be counted."""


async def read_goals(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext = _OWNER
) -> list[dict[str, Any]]:
    """Every player's goals, oldest first, and the newest JOURNAL_PER_GOAL journal entries
    of each goal (and of the player's entries for no goal), newest first."""
    async with scoped_session(maker, ctx) as s:
        goals = (
            await s.execute(
                text(
                    "SELECT id, xuid, gamertag, world, title, notes, status, item,"
                    " item_count, created_at, finished_at FROM app.mc_goals"
                    " ORDER BY xuid, created_at, id"
                )
            )
        ).all()
        log = (
            await s.execute(
                text(
                    "SELECT id, xuid, goal_id, at, text, source FROM (SELECT *, row_number()"
                    " OVER (PARTITION BY xuid, goal_id ORDER BY at DESC, id) AS n"
                    " FROM app.mc_goal_log) l WHERE n <= :n ORDER BY xuid, at DESC, id"
                ),
                {"n": JOURNAL_PER_GOAL},
            )
        ).all()
    players: dict[str, dict[str, Any]] = {}
    for g in goals:
        p = players.setdefault(g.xuid, {"xuid": g.xuid, "gamertag": g.gamertag, "goals": []})
        p["gamertag"] = g.gamertag
        p["goals"].append(
            {
                "id": str(g.id),
                "title": g.title,
                "notes": g.notes,
                "status": g.status,
                "world": g.world,
                "item": g.item,
                "item_count": g.item_count,
                "created_at": g.created_at.timestamp(),
                "finished_at": g.finished_at.timestamp() if g.finished_at else None,
            }
        )
    for row in log:
        p = players.get(row.xuid)
        if p is None:
            continue
        p.setdefault("journal", []).append(
            {
                "id": str(row.id),
                "goal_id": str(row.goal_id) if row.goal_id else None,
                "at": row.at.timestamp(),
                "text": row.text,
                "source": row.source,
            }
        )
    for p in players.values():
        p.setdefault("journal", [])
    return list(players.values())


def world_names(worlds: dict[str, Any] | str) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """The loaded world's folder and each world's slot record by folder, from `/worlds`;
    (None, {}) when the sidecar couldn't answer."""
    if isinstance(worlds, str):
        return None, {}
    slots = worlds.get("slots") or []
    by_folder = {str(r["folder"]): r for r in slots if r.get("exists") and r.get("folder")}
    return (str(worlds.get("active") or "") or None), by_folder


async def _player_rows(s: AsyncSession, xuid: str) -> tuple[list[Any], list[Any]]:
    goals = (
        await s.execute(
            text(
                "SELECT id, gamertag, world, item, item_count FROM app.mc_goals"
                " WHERE xuid = :k AND status = 'open' AND item IS NOT NULL"
                " ORDER BY created_at, id"
            ),
            {"k": xuid},
        )
    ).all()
    places = (
        await s.execute(
            text(
                "SELECT name, world, x, y, z, dim FROM app.mc_places WHERE xuid = :k"
                " ORDER BY lower(name)"
            ),
            {"k": xuid},
        )
    ).all()
    return list(goals), list(places)


async def count_progress(
    maker: async_sessionmaker[AsyncSession],
    sidecar: Sidecar,
    xuid: str,
    *,
    flush: bool,
    ctx: SessionContext = _OWNER,
) -> dict[str, Any]:
    """One player's counted open goals, counted now. `flush` asks the running world to
    save first (the sidecar does that at most every 30 s); without it the files are read
    as they are. Raises Unavailable when the sidecar can't list the worlds."""
    worlds = await sidecar("GET", "/worlds")
    if isinstance(worlds, str):
        raise Unavailable(worlds)
    active, by_folder = world_names(worlds)
    async with scoped_session(maker, ctx) as s:
        goals, places = await _player_rows(s, xuid)
    status = await sidecar("GET", "/status")
    online_as: str | None = None
    if isinstance(status, dict):
        for p in status.get("players") or []:
            name = str(p.get("name", ""))
            if sessions.player_key(str(p.get("xuid", "")), name) == xuid:
                online_as = name
    out: list[dict[str, Any]] = []
    by_world: dict[str, list[Any]] = {}
    for g in goals:
        by_world.setdefault(str(g.world or active or ""), []).append(g)
    for folder, group in by_world.items():
        out.extend(
            await _count_world(
                sidecar,
                folder,
                by_folder.get(folder),
                loaded=bool(folder) and folder == active,
                online_as=online_as,
                goals=group,
                places=[p for p in places if p.world == folder],
                flush=flush,
            )
        )
    order = {str(g.id): i for i, g in enumerate(goals)}
    out.sort(key=lambda r: order[r["id"]])
    return {"xuid": xuid, "online": online_as is not None, "goals": out}


async def _count_world(
    sidecar: Sidecar,
    folder: str,
    rec: dict[str, Any] | None,
    *,
    loaded: bool,
    online_as: str | None,
    goals: list[Any],
    places: list[Any],
    flush: bool,
) -> list[dict[str, Any]]:
    """One world's share of a player's goals: one carry and one stock count for all of
    them, then each goal's own numbers out of those."""
    items = sorted({full_id(str(g.item)) for g in goals})
    pocket: Pocket | None = None
    carried_why: str | None = None
    if not loaded:
        carried_why = "not_loaded"
    elif online_as is None:
        carried_why = "offline"
    else:
        got = await carry_pocket(sidecar, online_as, items)
        if isinstance(got, str):
            carried_why = "failed"
        else:
            pocket = got
    named = [(str(p.name), Spot(int(p.x), p.y, int(p.z), str(p.dim))) for p in places]
    # Places first, as Dave counts: a chest as near a place as the player goes to the place.
    centres = list(named) + ([("", pocket.spot)] if pocket is not None else [])
    chests_why: str | None = None
    detail: str | None = None
    stock: Any = None
    if rec is None:
        chests_why = "no_world"
    elif not centres:
        chests_why = "no_places"
    else:
        got_stock = await stock_near(
            sidecar, str(rec["id"]), [sp for _, sp in centres], items, flush=flush
        )
        if isinstance(got_stock, str):
            chests_why, detail = "failed", got_stock
        else:
            stock = got_stock
    rows: list[dict[str, Any]] = []
    for g in goals:
        item = full_id(str(g.item))
        carried = pocket.counts.get(item, 0) if pocket is not None else None
        chests: list[dict[str, Any]] = []
        here: int | None = None
        if stock is not None:
            for i, (name, sp) in enumerate(centres):
                n = stock.per[i].get(item, 0)
                if i >= len(named):
                    here = n
                    continue
                gap = _gap(pocket.spot if pocket is not None else None, sp)
                chests.append(
                    {
                        "place": name,
                        "count": n,
                        "distance": None if gap is None else round(gap),
                        # As Dave's in-game check decides it: beyond NEAR_BLOCKS of them, or
                        # in another dimension. Unknown (they're not on) is never far.
                        "far": pocket is not None and (gap is None or gap > NEAR_BLOCKS),
                    }
                )
        counted = [carried] if carried is not None else []
        if stock is not None:
            counted += [c["count"] for c in chests] + ([here] if here is not None else [])
        rows.append(
            {
                "id": str(g.id),
                "world": folder or None,
                "world_name": (rec or {}).get("name"),
                "loaded": loaded,
                "item": item,
                "need": int(g.item_count),
                "have": sum(counted) if counted else None,
                "carried": carried,
                "carried_why": carried_why,
                "boxes": pocket.boxes if pocket is not None else 0,
                "chests": chests,
                "here": here,
                "chests_why": chests_why,
                "detail": detail,
                "age_s": stock.age_s if stock is not None else None,
                "as_of": stock.as_of if stock is not None else None,
                "flush": stock.flush if stock is not None else None,
            }
        )
    return rows


def _gap(stand: Spot | None, place: Spot) -> float | None:
    """Blocks across from where the player stands to a place; None in another dimension
    or when where they stand isn't known."""
    if stand is None or stand.dim != place.dim:
        return None
    return math.dist((stand.x, stand.z), (place.x, place.z))
