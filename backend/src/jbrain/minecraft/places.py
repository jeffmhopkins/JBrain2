"""Saved places as the map sees them (GUI gate `docs/mocks/minecraft-map-layers/`, places
variant A, owner 2026-10-13; `app.mc_places`, migration 0228).

Every player's places in one world folder, each with what its card says and its list row
shows: whether a No-Spawn Beacon covers it (`markers.covers`, the pack's own chunk rule),
and which wormhole gates lead to it.

A gate keeps its own copy of where it was aimed (`target_x/y/z`), and saving a place again
moves the place without re-aiming the gate. So a gate leads *here* only when its target is
this place's block, in this dimension (the review's X1). A gate still named for the place
but aimed where it used to be is `gates_moved`: the map draws both, and the card says the
gate leads to where the place was.

A place is coloured and named by its player's newest gamertag from the play sessions, not
the one the row was saved under, so a renamed player keeps their own colour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import markers

_OWNER = SessionContext(principal_kind="owner")

_PLACES = text(
    """
    SELECT p.id, p.xuid, coalesce(cur.gamertag, p.gamertag) AS gamertag,
           p.gamertag AS saved_as, p.name,
           p.x, p.y, p.z, p.dim, p.created_at, p.updated_at
      FROM app.mc_places p
      LEFT JOIN LATERAL (
            SELECT s.gamertag FROM app.mc_player_sessions s
             WHERE s.xuid = p.xuid ORDER BY s.started_at DESC LIMIT 1) cur ON true
     WHERE p.world = :w
     ORDER BY lower(coalesce(cur.gamertag, p.gamertag)), lower(p.name)
    """
)


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def place_view(row: Any, beacons: list[dict[str, Any]], gates: list[dict[str, Any]]) -> dict:
    """One place as the map draws it."""
    leads: list[int] = []
    moved: list[int] = []
    for g in gates:
        t = g["target"]
        if g["state"] != "open" or t is None:
            continue
        # By block: a mine saved under a house shares its X/Z. A place saved without a
        # height can only be matched by its column.
        if (
            t["dim"] == row.dim
            and t["x"] == row.x
            and t["z"] == row.z
            and (row.y is None or t["y"] == row.y)
        ):
            leads.append(g["id"])
        elif _same(t["place"], row.name) and any(
            _same(g["planter"], who) for who in (row.gamertag, row.saved_as)
        ):
            moved.append(g["id"])
    return {
        "id": str(row.id),
        "xuid": row.xuid,
        "gamertag": row.gamertag,
        "name": row.name,
        "dim": row.dim,
        "x": row.x,
        "y": row.y,
        "z": row.z,
        # Saving a name again elsewhere moves the place: `updated_at` is when it was put
        # here, and a re-save on the same block leaves it (`mc_place_save`).
        "saved_at": _epoch(row.updated_at),
        "moved": row.updated_at > row.created_at,
        "covered": [
            {"x": b["x"], "y": b["y"], "z": b["z"], "placed_by": b["placed_by"]}
            for b in beacons
            if markers.covers(b, row.dim, row.x, row.z)
        ],
        "gates": leads,
        "gates_moved": moved,
    }


async def read(s: AsyncSession, world: str, dim: str | None = None) -> dict[str, Any]:
    """A world's saved places, in one dimension or all, and when the newest was saved."""
    rows = (await s.execute(_PLACES, {"w": world})).all()
    got = await markers.read(s, world)
    places = [
        place_view(r, got["beacons"], got["gates"]) for r in rows if dim is None or r.dim == dim
    ]
    return {
        "places": places,
        "as_of": max((p["saved_at"] for p in places), default=None),
    }


async def for_world(
    maker: async_sessionmaker[AsyncSession],
    world: str,
    dim: str | None = None,
    ctx: SessionContext = _OWNER,
) -> dict[str, Any]:
    """`read` in its own owner session, for the map route. Places are owner-only: a share
    link's principal has no policy on `app.mc_places`, so it can't read them."""
    async with scoped_session(maker, ctx) as s:
        return await read(s, world, dim)
