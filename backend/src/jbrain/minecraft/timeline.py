"""The map's timeline: one world's trails, events, sessions and fog over a time range.

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M8a. The PWA scrubs and replays locally — no
request per frame — so one answer carries everything the range needs, bounded so a
phone can hold it: each player's trail is cut to at most `MAX_POINTS`, never across a
session, a dimension or a respawn, so the client can interpolate inside a segment and
must not draw a line between two.

Sessions (`mc_player_sessions`) carry no world; a session belongs to the world its join
event (`mc_player_events`, same boot and event number) was recorded in. Sessions from
before the travel log existed have no such event and are not listed.
"""

from __future__ import annotations

import bisect
import json
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

_OWNER = SessionContext(principal_kind="owner")

# What a phone holds for one range: a player's trail over a week is ~60k samples, and
# 2,000 points still draw it at any zoom the screen can show.
MAX_POINTS = 2_000
# The longest range one request may cover, and the raw rows it may read on the way.
MAX_SPAN_S = 92 * 86_400
MAX_ROWS = 300_000
MAX_EVENTS = 5_000
MAX_SESSIONS = 500
MAX_CHUNKS = 60_000
MAX_PLAYERS = 16
# Samples with no session to place them in are split where the player went quiet this
# long: a session's own samples can be far apart while they stand still.
LOOSE_GAP_S = 1_800.0
# A respawn marks the jump from the death to the bed; a dimension change seen this soon
# after one is that same jump, not a portal.
RESPAWN_SLACK_S = 15.0

PARTS = frozenset({"trails", "events", "sessions", "explored"})
DIMS = ("overworld", "nether", "the_end")


@dataclass
class _Seg:
    session: str | None
    dim: str
    points: list[list[float]] = field(default_factory=list)


def _span_ok(start: float, end: float) -> None:
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("the range must end after it starts")
    if end - start > MAX_SPAN_S:
        raise ValueError(f"a range covers at most {MAX_SPAN_S // 86_400} days")


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def downsample(segments: list[_Seg], cap: int = MAX_POINTS) -> tuple[list[_Seg], bool]:
    """At most `cap` points across all segments, keeping every segment's first and last
    point (where a session, dimension or life starts and ends) and spreading the rest of
    the budget over each segment's interior in proportion to its length. Returns the
    segments and whether anything was dropped."""
    total = sum(len(s.points) for s in segments)
    if total <= cap:
        return segments, False
    ends = sum(min(2, len(s.points)) for s in segments)
    if ends > cap:
        # More segments than the budget can bound: keep the newest whole ends.
        kept: list[_Seg] = []
        room = cap
        for s in reversed(segments):
            two = [s.points[0], s.points[-1]] if len(s.points) > 1 else s.points[:1]
            if len(two) > room:
                break
            kept.append(_Seg(s.session, s.dim, two))
            room -= len(two)
        return kept[::-1], True
    inner_total = total - ends
    budget = cap - ends
    out: list[_Seg] = []
    for s in segments:
        pts = s.points
        if len(pts) <= 2:
            out.append(_Seg(s.session, s.dim, list(pts)))
            continue
        inner = pts[1:-1]
        keep = int(len(inner) * budget / inner_total)
        picked = [inner[(i * len(inner)) // keep] for i in range(keep)] if keep else []
        out.append(_Seg(s.session, s.dim, [pts[0], *picked, pts[-1]]))
    return out, True


def segment(
    rows: Sequence[Any],
    sessions: list[tuple[float, float, str]],
    respawns: list[float],
) -> list[_Seg]:
    """One player's samples, in time order, cut into segments: a new one starts with a
    new session, a new dimension, or after a respawn. `sessions` is (start, end, id) by
    start; `respawns` the player's respawn times, sorted."""
    starts = [s[0] for s in sessions]
    segs: list[_Seg] = []
    prev_at: float | None = None
    for r in rows:
        at = float(r.at)
        i = bisect.bisect_right(starts, at) - 1
        sid = sessions[i][2] if i >= 0 and at <= sessions[i][1] else None
        last = segs[-1] if segs else None
        respawned = prev_at is not None and bisect.bisect_right(respawns, at) > bisect.bisect_right(
            respawns, prev_at
        )
        loose_gap = sid is None and prev_at is not None and at - prev_at > LOOSE_GAP_S
        if last is None or last.session != sid or last.dim != r.dim or respawned or loose_gap:
            last = _Seg(sid, str(r.dim))
            segs.append(last)
        last.points.append([round(at, 1), round(r.x, 1), round(r.y, 1), round(r.z, 1)])
        prev_at = at
    return segs


TRACK_SQL = text(
    """
    SELECT xuid, gamertag, extract(epoch FROM at) AS at, dim, x, y, z
      FROM app.mc_player_track
     WHERE world = :world AND at >= :start AND at <= :end
       AND (:everyone OR xuid = ANY(CAST(:players AS text[])))
     ORDER BY xuid, at
     LIMIT :cap
    """
)

# Every session of these players overlapping the range, whatever world: a player is in
# one world at a time, so it places their samples without needing the join event.
SESSION_SPANS_SQL = text(
    """
    SELECT id::text AS id, xuid,
           extract(epoch FROM started_at) AS started,
           extract(epoch FROM coalesce(ended_at, greatest(last_seen_at, now()))) AS finished
      FROM app.mc_player_sessions
     WHERE xuid = ANY(CAST(:players AS text[]))
       AND started_at <= :end AND coalesce(ended_at, now()) >= :start
     ORDER BY started_at
    """
)

EVENTS_SQL = text(
    """
    SELECT xuid, gamertag, extract(epoch FROM at) AS at, kind, dim, x, y, z, detail
      FROM app.mc_player_events
     WHERE world = :world AND at >= :start AND at <= :end
       AND kind IN ('join', 'leave', 'death', 'respawn')
       AND (:everyone OR xuid = ANY(CAST(:players AS text[])))
     ORDER BY at
     LIMIT :cap
    """
)

# The world's sessions, newest first, each with its deaths and the end it has so far.
SESSIONS_SQL = text(
    """
    SELECT s.id::text AS id, s.xuid, s.gamertag,
           extract(epoch FROM s.started_at) AS started,
           extract(epoch FROM s.ended_at) AS ended,
           extract(epoch FROM s.last_seen_at) AS last_seen,
           (SELECT count(*) FROM app.mc_player_events d
             WHERE d.world = j.world AND d.xuid = s.xuid AND d.kind = 'death'
               AND d.at >= s.started_at AND d.at <= coalesce(s.ended_at, now())) AS deaths
      FROM app.mc_player_sessions s
      JOIN app.mc_player_events j
        ON j.boot_id = s.boot_id AND j.event_id = s.start_event_id AND j.kind = 'join'
     WHERE j.world = :world
     ORDER BY s.started_at DESC
     LIMIT :cap
    """
)

# Distance per session and dimension: the straight steps between consecutive samples in
# one dimension, never the jump a respawn makes from the death to the bed.
SESSION_TRAVEL_SQL = text(
    """
    WITH ses AS (
        SELECT id, xuid, started_at, coalesce(ended_at, now()) AS finished
          FROM app.mc_player_sessions WHERE id = ANY(CAST(:ids AS uuid[]))
    ), steps AS (
        SELECT ses.id, ses.xuid, t.dim, t.at, t.x, t.z,
               lag(t.dim) OVER w AS pdim, lag(t.x) OVER w AS px,
               lag(t.z) OVER w AS pz, lag(t.at) OVER w AS pat
          FROM ses JOIN app.mc_player_track t
            ON t.world = :world AND t.xuid = ses.xuid
           AND t.at >= ses.started_at AND t.at <= ses.finished
        WINDOW w AS (PARTITION BY ses.id ORDER BY t.at)
    )
    SELECT id::text AS id, dim,
           sum(CASE WHEN pdim = dim AND NOT EXISTS (
                   SELECT 1 FROM app.mc_player_events r
                    WHERE r.world = :world AND r.xuid = steps.xuid AND r.kind = 'respawn'
                      AND r.at > steps.pat AND r.at <= steps.at)
               THEN sqrt((x - px) ^ 2 + (z - pz) ^ 2) ELSE 0 END) AS distance,
           extract(epoch FROM min(at)) AS first_at,
           (array_agg(x ORDER BY at))[1] AS first_x,
           (array_agg(z ORDER BY at))[1] AS first_z
      FROM steps
     GROUP BY id, dim
    """
)

EXPLORED_SQL = text(
    """
    SELECT cx, cz, extract(epoch FROM min(first_seen)) AS first_seen
      FROM app.mc_player_explored
     WHERE world = :world AND dim = :dim AND first_seen <= :end
       AND (:everyone OR xuid = ANY(CAST(:players AS text[])))
     GROUP BY cx, cz
     ORDER BY first_seen
     LIMIT :cap
    """
)


def _place(ev: dict[str, Any], samples: list[list[float]], dims: list[str]) -> None:
    """A join or leave carries no position, and a death can arrive without one: put it at
    the player's sample nearest in the right direction — the first one after a join, the
    last one before anything else."""
    if ev["x"] is not None and ev["z"] is not None:
        return
    times = [p[0] for p in samples]
    at = ev["at"]
    after = bisect.bisect_left(times, at)
    before = bisect.bisect_right(times, at) - 1
    order = (after, before) if ev["kind"] == "join" else (before, after)
    for i in order:
        if 0 <= i < len(samples):
            _t, x, y, z = samples[i]
            ev.update(x=x, y=y, z=z, dim=ev["dim"] or dims[i])
            return


async def timeline(
    maker: async_sessionmaker[AsyncSession],
    world: str,
    start: float,
    end: float,
    *,
    players: Sequence[str] = (),
    include: frozenset[str] = frozenset({"trails", "events", "sessions"}),
    dim: str = "overworld",
    ctx: SessionContext = _OWNER,
) -> dict[str, Any]:
    """Everything the map's timeline draws for one world folder over [start, end]
    (epoch seconds): see the module docstring for the shape and its bounds. A map share
    reads it under its own pinned `ctx`."""
    _span_ok(start, end)
    if dim not in DIMS:
        raise ValueError("dimension must be overworld, nether or the_end")
    if len(players) > MAX_PLAYERS:
        raise ValueError(f"at most {MAX_PLAYERS} players at a time")
    who = {"everyone": not players, "players": list(players)}
    rng = {"world": world, "start": _ts(start), "end": _ts(end)}
    out: dict[str, Any] = {
        "world": world,
        "from": start,
        "to": end,
        "now": datetime.now(UTC).timestamp(),
    }
    async with scoped_session(maker, ctx) as s:
        if "trails" in include or "events" in include:
            trails, events, cut = await _trails_and_events(s, rng, who)
            if "trails" in include:
                out["trails"] = trails
                out["truncated"] = cut
            if "events" in include:
                out["events"] = events
        if "sessions" in include:
            out["sessions"] = await _sessions(s, world)
        if "explored" in include:
            rows = (
                await s.execute(EXPLORED_SQL, {**rng, **who, "dim": dim, "cap": MAX_CHUNKS})
            ).all()
            out["explored"] = {
                "dim": dim,
                "chunks": [[r.cx, r.cz, round(float(r.first_seen), 1)] for r in rows],
                "truncated": len(rows) >= MAX_CHUNKS,
            }
    return out


async def _trails_and_events(
    s: AsyncSession, rng: dict[str, Any], who: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    rows = (await s.execute(TRACK_SQL, {**rng, **who, "cap": MAX_ROWS})).all()
    raw_events = (await s.execute(EVENTS_SQL, {**rng, **who, "cap": MAX_EVENTS})).all()
    by_player: dict[str, list[Any]] = {}
    for r in rows:
        by_player.setdefault(r.xuid, []).append(r)
    spans: dict[str, list[tuple[float, float, str]]] = {}
    if by_player:
        got = await s.execute(SESSION_SPANS_SQL, {**rng, "players": list(by_player)})
        for r in got:
            spans.setdefault(r.xuid, []).append((float(r.started), float(r.finished), r.id))
    respawns: dict[str, list[float]] = {}
    for e in raw_events:
        if e.kind == "respawn":
            respawns.setdefault(e.xuid, []).append(float(e.at))

    trails: list[dict[str, Any]] = []
    flat: dict[str, tuple[list[list[float]], list[str]]] = {}
    derived: list[dict[str, Any]] = []
    for xuid, prs in by_player.items():
        segs = segment(prs, spans.get(xuid, []), respawns.get(xuid, []))
        flat[xuid] = (
            [p for g in segs for p in g.points],
            [g.dim for g in segs for _ in g.points],
        )
        gamertag = prs[-1].gamertag
        for a, b in zip(segs, segs[1:], strict=False):
            if a.session is None or a.session != b.session or a.dim == b.dim:
                continue
            at = b.points[0][0]
            if any(a.points[-1][0] <= r <= at + RESPAWN_SLACK_S for r in respawns.get(xuid, [])):
                continue
            _t, x, y, z = b.points[0]
            derived.append(
                {
                    "kind": "dimension",
                    "xuid": xuid,
                    "gamertag": gamertag,
                    "at": at,
                    "dim": b.dim,
                    "from_dim": a.dim,
                    "x": x,
                    "y": y,
                    "z": z,
                }
            )
        kept, cut = downsample(segs)
        trails.append(
            {
                "xuid": xuid,
                "gamertag": gamertag,
                "samples": len(prs),
                "downsampled": cut,
                "segments": [
                    {"session": g.session, "dim": g.dim, "points": g.points} for g in kept
                ],
            }
        )

    events: list[dict[str, Any]] = []
    for e in raw_events:
        detail = e.detail if isinstance(e.detail, dict) else json.loads(e.detail or "{}")
        ev = {
            "kind": e.kind,
            "xuid": e.xuid,
            "gamertag": e.gamertag,
            "at": round(float(e.at), 1),
            "dim": e.dim,
            "x": e.x,
            "y": e.y,
            "z": e.z,
        }
        if e.kind == "death":
            ev["cause"] = detail.get("cause") or None
            ev["killer"] = detail.get("killer") or None
        samples, dims = flat.get(e.xuid, ([], []))
        _place(ev, samples, dims)
        events.append(ev)
    events.extend(derived)
    events.sort(key=lambda v: v["at"])
    return trails, events[:MAX_EVENTS], len(rows) >= MAX_ROWS


async def _sessions(s: AsyncSession, world: str) -> list[dict[str, Any]]:
    rows = (await s.execute(SESSIONS_SQL, {"world": world, "cap": MAX_SESSIONS})).all()
    if not rows:
        return []
    travel: dict[str, dict[str, Any]] = {}
    ids = [uuid.UUID(r.id) for r in rows]
    got = await s.execute(SESSION_TRAVEL_SQL, {"world": world, "ids": ids})
    for r in got:
        t = travel.setdefault(r.id, {"distance": {}, "first": None, "first_at": math.inf})
        t["distance"][r.dim] = round(float(r.distance or 0))
        if float(r.first_at) < t["first_at"]:
            t["first_at"] = float(r.first_at)
            t["first"] = {"dim": r.dim, "x": round(r.first_x, 1), "z": round(r.first_z, 1)}
    out = []
    for r in rows:
        t = travel.get(r.id, {"distance": {}, "first": None})
        out.append(
            {
                "id": r.id,
                "xuid": r.xuid,
                "gamertag": r.gamertag,
                "start": float(r.started),
                # Open is "on now"; the screen measures it to its own clock.
                "end": float(r.ended) if r.ended is not None else None,
                "last_seen": float(r.last_seen),
                "distance": t["distance"],
                "dims": [d for d in DIMS if d in t["distance"]],
                "deaths": int(r.deaths),
                "first": t["first"],
            }
        )
    return out


async def counts(
    maker: async_sessionmaker[AsyncSession], world: str, start: float, end: float
) -> dict[str, Any]:
    """The debug view of a timeline: how much each player's trail holds and what the
    phone would be sent, with no position in it."""
    got = await timeline(maker, world, start, end, include=frozenset({"trails", "events"}))
    kinds: dict[str, int] = {}
    for e in got["events"]:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    async with scoped_session(maker, _OWNER) as s:
        sessions = (await s.execute(SESSIONS_SQL, {"world": world, "cap": MAX_SESSIONS})).all()
    return {
        "world": world,
        "from": start,
        "to": end,
        "players": [
            {
                "xuid": t["xuid"],
                "gamertag": t["gamertag"],
                "samples": t["samples"],
                "points_sent": sum(len(g["points"]) for g in t["segments"]),
                "segments": len(t["segments"]),
                "downsampled": t["downsampled"],
            }
            for t in got["trails"]
        ],
        "events": kinds,
        "sessions": len(sessions),
        "truncated": got["truncated"],
    }
