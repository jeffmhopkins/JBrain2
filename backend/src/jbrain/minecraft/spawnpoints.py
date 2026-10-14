"""Each player's respawn point as the owner's map and Dave see it (plan §M8, beds;
`app.mc_spawn_points`, migration 0246).

The pack sends a `spawnpoint` line when a player's point changes and on every join; the
session drain (`sessions.SessionDrain`) hands each to `apply`. A line that repeats the
player's latest row adds nothing, so the table is the history of changes; re-reading
events after an api restart adds nothing either. A reset or import of the world clears it.

Owner-only in RLS: a map share link never reads it (review X3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

_OWNER = SessionContext(principal_kind="owner")

KINDS = ("bed", "anchor", "other", "none")
# The pack couldn't read the blocks at a point it hadn't seen this session (a join far
# from home, a reload): judged here against the stored rows, never stored itself.
UNKNOWN = "unknown"
DIMS = ("overworld", "nether", "the_end")
# How far back the map's death cards look: the live view's week, and a day to spare.
LOG_DAYS = 8

_INSERT = text(
    """
    INSERT INTO app.mc_spawn_points
        (world, xuid, gamertag, boot_id, event_id, at, kind, dim, x, y, z)
    SELECT :w, :key, :name, :boot, :eid, :at, :kind, :dim, :x, :y, :z
     WHERE NOT EXISTS (
        SELECT 1 FROM (
            SELECT kind, dim, x, y, z FROM app.mc_spawn_points
             WHERE world = :w AND xuid = :key AND at <= :at
             ORDER BY at DESC, id DESC LIMIT 1
        ) latest
         WHERE latest.kind = :kind
           AND latest.dim IS NOT DISTINCT FROM CAST(:dim AS text)
           AND latest.x IS NOT DISTINCT FROM CAST(:x AS integer)
           AND latest.y IS NOT DISTINCT FROM CAST(:y AS integer)
           AND latest.z IS NOT DISTINCT FROM CAST(:z AS integer))
    ON CONFLICT (boot_id, event_id) DO NOTHING
    """
)


_LATEST = text(
    "SELECT kind, dim, x, y, z FROM app.mc_spawn_points"
    " WHERE world = :w AND xuid = :key AND at <= :at ORDER BY at DESC, id DESC LIMIT 1"
)


def _key(e: dict[str, Any]) -> str:
    # The travel log's player key (sessions.player_key), kept local to avoid the cycle.
    xuid = str(e.get("xuid") or "")
    return xuid or f"name:{e.get('name', '')}"


async def apply(s: AsyncSession, boot: str, e: dict[str, Any], at: datetime) -> None:
    """One drained event: a player's spawn point, or a world replaced."""
    kind = e.get("kind")
    world = str(e.get("world") or "world")
    if kind == "world_replaced":
        await s.execute(
            text("DELETE FROM app.mc_spawn_points WHERE world = :w AND at <= :at"),
            {"w": world, "at": at},
        )
        return
    if kind != "spawnpoint":
        return
    spawn = e.get("spawn")
    name = str(e.get("name") or "")
    if (spawn not in KINDS and spawn != UNKNOWN) or not name:
        return
    place: dict[str, Any] = {"dim": None, "x": None, "y": None, "z": None}
    if spawn != "none":
        try:
            place = {"dim": str(e["dim"]), **{k: int(e[k]) for k in ("x", "y", "z")}}
        except (KeyError, TypeError, ValueError):
            return
        if place["dim"] not in DIMS:
            return
    if spawn == UNKNOWN:
        latest = (await s.execute(_LATEST, {"w": world, "key": _key(e), "at": at})).first()
        if latest is not None and (latest.dim, latest.x, latest.y, latest.z) == (
            place["dim"],
            place["x"],
            place["y"],
            place["z"],
        ):
            return  # the point it already holds: nothing changed
        # A point never seen with a bed or anchor at it is one with none seen there.
        spawn = "other"
    await s.execute(
        _INSERT,
        {
            "w": world,
            "key": _key(e),
            "name": name,
            "boot": boot,
            "eid": int(e["id"]),
            "at": at,
            "kind": spawn,
            **place,
        },
    )


def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def _point(r: Any) -> dict[str, Any]:
    return {"kind": r.kind, "dim": r.dim, "x": r.x, "y": r.y, "z": r.z}


async def for_world(
    maker: async_sessionmaker[AsyncSession],
    world: str,
    ctx: SessionContext = _OWNER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """`read` in its own session, for the map route."""
    async with scoped_session(maker, ctx) as s:
        return await read(s, world, now)


async def read(s: AsyncSession, world: str, now: datetime | None = None) -> dict[str, Any]:
    """A world's respawn points: `spawns`, each player's current one (`none` included, so
    "no bed set" is said only when the pack said so), with `set_at` when they got that
    point; and `spawn_log`, every change of the last LOG_DAYS plus the one in effect
    before them, for matching each death's waking to the point they had then. Times are
    epoch seconds."""
    rows = (
        await s.execute(
            text(
                "SELECT xuid, gamertag, at, kind, dim, x, y, z FROM app.mc_spawn_points"
                " WHERE world = :w ORDER BY xuid, at, id"
            ),
            {"w": world},
        )
    ).all()
    cutoff = (now or datetime.now(UTC)) - timedelta(days=LOG_DAYS)
    by: dict[str, list[Any]] = {}
    for r in rows:
        by.setdefault(r.xuid, []).append(r)
    spawns: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    for xuid, mine in by.items():
        last = mine[-1]
        set_at = last.at
        # Back through the run of rows at the same place: a bed read as `other` while it
        # was broken, then a bed again, was still set when it first was.
        for r in reversed(mine):
            if (r.dim, r.x, r.y, r.z) != (last.dim, last.x, last.y, last.z):
                break
            set_at = r.at
        spawns.append(
            {
                "xuid": xuid,
                "gamertag": last.gamertag,
                **_point(last),
                "set_at": _epoch(set_at),
                "at": _epoch(last.at),
            }
        )
        recent = [i for i, r in enumerate(mine) if r.at >= cutoff]
        start = max(0, (recent[0] if recent else len(mine)) - 1)
        log.extend({"xuid": xuid, "at": _epoch(r.at), **_point(r)} for r in mine[start:])
    spawns.sort(key=lambda p: str(p["gamertag"]).casefold())
    log.sort(key=lambda r: r["at"])
    return {"spawns": spawns, "spawn_log": log}


async def current(s: AsyncSession, world: str, xuid: str) -> Any:
    """One player's current respawn point in a world, or None when the pack never sent
    one."""
    return (
        await s.execute(
            text(
                "SELECT gamertag, kind, dim, x, y, z, extract(epoch FROM at) AS at"
                " FROM app.mc_spawn_points WHERE world = :w AND xuid = :k"
                " ORDER BY at DESC, id DESC LIMIT 1"
            ),
            {"w": world, "k": xuid},
        )
    ).first()


async def current_by_name(s: AsyncSession, world: str, gamertag: str) -> Any:
    """`current`, for whoever last went by this gamertag in the world."""
    return (
        await s.execute(
            text(
                "SELECT xuid, gamertag, kind, dim, x, y, z, extract(epoch FROM at) AS at"
                " FROM app.mc_spawn_points p WHERE world = :w AND id = ("
                "   SELECT id FROM app.mc_spawn_points q"
                "    WHERE q.world = :w AND q.xuid = ("
                "      SELECT xuid FROM app.mc_spawn_points"
                "       WHERE world = :w AND lower(gamertag) = lower(:n)"
                "       ORDER BY at DESC LIMIT 1)"
                "    ORDER BY at DESC, id DESC LIMIT 1)"
            ),
            {"w": world, "n": gamertag},
        )
    ).first()
