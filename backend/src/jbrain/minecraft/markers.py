"""No-Spawn Beacons and wormhole gates as the map and Dave see them (plan §M26,
`app.mc_world_beacons` / `app.mc_world_gates`, migration 0239).

The pack sends `beacon` / `gate` events when one changes and `beacons` / `gates`
snapshots after each load; the session drain (`sessions.SessionDrain`) hands each to
`apply`, in order, inside its own transaction. An event patches a world's rows; a whole
snapshot replaces them (a cut one, over the sidecar's cap, only patches); a reset or import
of the world clears them. Re-reading events after an api restart replays them in the same
order, so the rows end where the last one left them.

The covered check is pure: a spot is protected when its chunk is within one chunk of a
beacon's in the same dimension — the pack's own rule (`beacon_rules.js`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

_OWNER = SessionContext(principal_kind="owner")

KINDS = ("beacon", "gate", "beacons", "gates")
CHUNK = 16
# A beacon covers its own chunk and one on each side.
REACH = 1

_BEACON_UPSERT = text(
    """
    INSERT INTO app.mc_world_beacons (world, dim, x, y, z, placed_by, placed_at, seen_at)
    VALUES (:w, :dim, :x, :y, :z, :by, :placed, :at)
    ON CONFLICT (world, dim, x, y, z) DO UPDATE
       SET placed_by = CASE WHEN excluded.placed_by <> '' THEN excluded.placed_by
                            ELSE app.mc_world_beacons.placed_by END,
           placed_at = coalesce(excluded.placed_at, app.mc_world_beacons.placed_at),
           seen_at = excluded.seen_at
    """
)

_GATE_UPSERT = text(
    """
    INSERT INTO app.mc_world_gates (world, gate_id, dim, x, y, z, axis, planter, state,
        target_place, target_dim, target_x, target_y, target_z, planted_at, aimed_at, seen_at)
    VALUES (:w, :id, :dim, :x, :y, :z, :axis, :planter, :state,
        :tp, :td, :tx, :ty, :tz, :planted, :aimed, :at)
    ON CONFLICT (world, gate_id) DO UPDATE
       SET dim = excluded.dim, x = excluded.x, y = excluded.y, z = excluded.z,
           axis = excluded.axis,
           planter = CASE WHEN excluded.planter <> '' THEN excluded.planter
                          ELSE app.mc_world_gates.planter END,
           state = excluded.state,
           target_place = excluded.target_place, target_dim = excluded.target_dim,
           target_x = excluded.target_x, target_y = excluded.target_y,
           target_z = excluded.target_z,
           planted_at = coalesce(excluded.planted_at, app.mc_world_gates.planted_at),
           -- A snapshot names no time: it keeps the last aim's unless the target moved
           -- under it (an aim the drain never heard), which leaves the time unknown.
           aimed_at = CASE
               WHEN excluded.aimed_at IS NOT NULL THEN excluded.aimed_at
               WHEN (app.mc_world_gates.target_place, app.mc_world_gates.target_dim,
                     app.mc_world_gates.target_x, app.mc_world_gates.target_y,
                     app.mc_world_gates.target_z)
                    IS NOT DISTINCT FROM
                    (excluded.target_place, excluded.target_dim, excluded.target_x,
                     excluded.target_y, excluded.target_z)
               THEN app.mc_world_gates.aimed_at
               ELSE NULL END,
           seen_at = excluded.seen_at
    """
)


def _int(v: Any) -> int:
    return int(v)


def _target(t: Any) -> dict[str, Any]:
    if not isinstance(t, dict) or not t.get("place"):
        return {"tp": None, "td": None, "tx": None, "ty": None, "tz": None}
    return {
        "tp": str(t["place"]),
        "td": str(t["dim"]),
        "tx": _int(t["x"]),
        "ty": _int(t["y"]),
        "tz": _int(t["z"]),
    }


async def _beacon(
    s: AsyncSession, world: str, b: dict[str, Any], at: datetime, *, placed: bool
) -> None:
    await s.execute(
        _BEACON_UPSERT,
        {
            "w": world,
            "dim": str(b["dim"]),
            "x": _int(b["x"]),
            "y": _int(b["y"]),
            "z": _int(b["z"]),
            "by": str(b.get("by") or ""),
            "placed": at if placed else None,
            "at": at,
        },
    )


async def _gate(
    s: AsyncSession,
    world: str,
    g: dict[str, Any],
    at: datetime,
    *,
    planted: bool = False,
    aimed: bool = False,
) -> None:
    tgt = _target(g.get("target"))
    await s.execute(
        _GATE_UPSERT,
        {
            "w": world,
            "id": _int(g["id"]),
            "dim": str(g["dim"]),
            "x": _int(g["x"]),
            "y": _int(g["y"]),
            "z": _int(g["z"]),
            "axis": str(g["axis"]),
            "planter": str(g.get("planter") or ""),
            "state": "open" if tgt["tp"] else "dormant",
            **tgt,
            "planted": at if planted else None,
            "aimed": at if aimed and tgt["tp"] else None,
            "at": at,
        },
    )


async def apply(s: AsyncSession, e: dict[str, Any], at: datetime) -> None:
    """One drained event: a beacon or gate change, a snapshot, or a world replaced."""
    kind = e.get("kind")
    world = str(e.get("world") or "world")
    if kind == "world_replaced":
        # Its terrain is gone, and its beacons and gates with it. A snapshot from the new
        # world, drained after this, fills them again.
        for table in ("mc_world_beacons", "mc_world_gates"):
            await s.execute(
                text(f"DELETE FROM app.{table} WHERE world = :w AND seen_at <= :at"),
                {"w": world, "at": at},
            )
        return
    if kind not in KINDS:
        return
    try:
        await _apply(s, kind, world, e, at)
    except (KeyError, TypeError, ValueError):
        # The sidecar checked every field; a line it let through that still isn't whole is
        # dropped rather than stopping the drain behind it.
        return


async def _apply(s: AsyncSession, kind: str, world: str, e: dict[str, Any], at: datetime) -> None:
    if kind == "beacon":
        if e.get("op") == "on":
            await _beacon(s, world, e, at, placed=True)
        else:
            await s.execute(
                text(
                    "DELETE FROM app.mc_world_beacons"
                    " WHERE world = :w AND dim = :dim AND x = :x AND y = :y AND z = :z"
                ),
                {
                    "w": world,
                    "dim": e["dim"],
                    "x": _int(e["x"]),
                    "y": _int(e["y"]),
                    "z": _int(e["z"]),
                },
            )
    elif kind == "gate":
        op = e.get("op")
        if op == "collapsed":
            await s.execute(
                text("DELETE FROM app.mc_world_gates WHERE world = :w AND gate_id = :id"),
                {"w": world, "id": _int(e["id"])},
            )
        else:
            await _gate(s, world, e, at, planted=op == "planted", aimed=op in ("opened", "aimed"))
    else:
        items = [i for i in e.get("all") or [] if isinstance(i, dict)]
        if kind == "beacons":
            if not e.get("truncated"):
                keep = [(str(b["dim"]), _int(b["x"]), _int(b["y"]), _int(b["z"])) for b in items]
                await s.execute(
                    text(
                        "DELETE FROM app.mc_world_beacons WHERE world = :w"
                        " AND (dim, x, y, z) NOT IN (SELECT * FROM unnest("
                        "CAST(:d AS text[]), CAST(:x AS int[]), CAST(:y AS int[]),"
                        " CAST(:z AS int[])))"
                    ),
                    {
                        "w": world,
                        "d": [k[0] for k in keep],
                        "x": [k[1] for k in keep],
                        "y": [k[2] for k in keep],
                        "z": [k[3] for k in keep],
                    },
                )
            for b in items:
                await _beacon(s, world, b, at, placed=False)
        else:
            if not e.get("truncated"):
                await s.execute(
                    text(
                        "DELETE FROM app.mc_world_gates WHERE world = :w"
                        " AND NOT (gate_id = ANY(CAST(:ids AS int[])))"
                    ),
                    {"w": world, "ids": [_int(g["id"]) for g in items]},
                )
            for g in items:
                await _gate(s, world, g, at)


def _epoch(dt: datetime | None) -> float | None:
    return dt.timestamp() if dt is not None else None


async def for_world(
    maker: async_sessionmaker[AsyncSession],
    world: str,
    dim: str | None = None,
    ctx: SessionContext = _OWNER,
) -> dict[str, Any]:
    """`read` in its own session, for the map route."""
    async with scoped_session(maker, ctx) as s:
        return await read(s, world, dim)


async def read(s: AsyncSession, world: str, dim: str | None = None) -> dict[str, Any]:
    """A world's beacons and gates, in one dimension or all, and when the pack last
    reported any of them (`as_of`, epoch seconds; None when it never has)."""
    where = "world = :w" + (" AND dim = :d" if dim else "")
    params = {"w": world, "d": dim}
    beacons = (
        await s.execute(
            text(
                "SELECT dim, x, y, z, placed_by, placed_at, seen_at"
                f" FROM app.mc_world_beacons WHERE {where} ORDER BY placed_at NULLS LAST, x, z"
            ),
            params,
        )
    ).all()
    gates = (
        await s.execute(
            text(
                "SELECT gate_id, dim, x, y, z, axis, planter, state, target_place,"
                " target_dim, target_x, target_y, target_z, planted_at, aimed_at, seen_at"
                f" FROM app.mc_world_gates WHERE {where} ORDER BY gate_id"
            ),
            params,
        )
    ).all()
    seen = [r.seen_at for r in (*beacons, *gates)]
    return {
        "beacons": [
            {
                "dim": r.dim,
                "x": r.x,
                "y": r.y,
                "z": r.z,
                "placed_by": r.placed_by,
                "placed_at": _epoch(r.placed_at),
            }
            for r in beacons
        ],
        "gates": [
            {
                "id": r.gate_id,
                "dim": r.dim,
                "x": r.x,
                "y": r.y,
                "z": r.z,
                "axis": r.axis,
                "planter": r.planter,
                "state": r.state,
                "target": (
                    {
                        "place": r.target_place,
                        "dim": r.target_dim,
                        "x": r.target_x,
                        "y": r.target_y,
                        "z": r.target_z,
                    }
                    if r.target_place is not None
                    else None
                ),
                "planted_at": _epoch(r.planted_at),
                "aimed_at": _epoch(r.aimed_at),
            }
            for r in gates
        ],
        "as_of": _epoch(max(seen)) if seen else None,
    }


async def summary(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext = _OWNER
) -> list[dict[str, Any]]:
    """Per world: how many beacons, open and dormant gates the drain holds, and the newest
    report — counts only, for the debug route, the travel log's rule."""
    async with scoped_session(maker, ctx) as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT world, sum(beacons)::int AS beacons, sum(open)::int AS open,
                           sum(dormant)::int AS dormant, max(seen_at) AS seen_at
                      FROM (SELECT world, 1 AS beacons, 0 AS open, 0 AS dormant, seen_at
                              FROM app.mc_world_beacons
                            UNION ALL
                            SELECT world, 0, (state = 'open')::int, (state = 'dormant')::int,
                                   seen_at
                              FROM app.mc_world_gates) m
                     GROUP BY world ORDER BY world
                    """
                )
            )
        ).all()
    return [
        {
            "world": r.world,
            "beacons": r.beacons,
            "gates_open": r.open,
            "gates_dormant": r.dormant,
            "as_of": _epoch(r.seen_at),
        }
        for r in rows
    ]


def chunk_of(v: float) -> int:
    return int(v // CHUNK)


def covers(beacon: dict[str, Any], dim: str, x: float, z: float) -> bool:
    """Whether a beacon protects the block at (x, z) in `dim`: its chunk is within one
    chunk of the beacon's, full height."""
    return (
        beacon["dim"] == dim
        and abs(chunk_of(x) - chunk_of(beacon["x"])) <= REACH
        and abs(chunk_of(z) - chunk_of(beacon["z"])) <= REACH
    )


def area_of(beacon: dict[str, Any]) -> tuple[int, int, int, int]:
    """The protected square's first and last block, x0, z0, x1, z1 (48 × 48)."""
    cx, cz = chunk_of(beacon["x"]), chunk_of(beacon["z"])
    return (
        (cx - REACH) * CHUNK,
        (cz - REACH) * CHUNK,
        (cx + REACH + 1) * CHUNK - 1,
        (cz + REACH + 1) * CHUNK - 1,
    )
