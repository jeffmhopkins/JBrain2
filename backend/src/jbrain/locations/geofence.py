"""Inline geofence detection for an incoming fix (Phase 7 Wave 3b).

On each new fix we evaluate the subject's applicable fences (its own + the
subject-less "all devices" fences, both visible under the device session's RLS),
update the per-(subject, fence) hysteresis state, and emit a
`location.geofence_transition` workflow event on a real enter/exit. Events only:
crossing a fence is a fact about the world the engine can react to; no note or
graph fact is auto-authored here (notes are the sole sources of truth, #7).

Two debounce rules keep a phone parked on a fence edge from flapping:
- entering requires `CONFIRM_FIXES` consecutive inside fixes;
- leaving requires the fix to be clearly outside (beyond `radius + EXIT_BUFFER_M`)
  for `CONFIRM_FIXES` fixes.
A low-accuracy fix (`accuracy_m > ACCURACY_GATE_M`) is ignored for detection — it
would otherwise smear the boundary.
"""

from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import device_context, scoped_session
from jbrain.workflow import events as wf_events

log = structlog.get_logger()

GEOFENCE_TRANSITION = "location.geofence_transition"

CONFIRM_FIXES = 2  # consecutive confirming fixes to flip state (Wave 0 default)
EXIT_BUFFER_M = 50.0  # leave only when beyond radius + this (hysteresis)
ACCURACY_GATE_M = 100.0  # drop fixes worse than this from detection


@dataclass(frozen=True)
class FenceObs:
    """What this fix says about one fence: inside it, and within its exit buffer."""

    inside: bool
    inside_buffered: bool


@dataclass(frozen=True)
class FenceState:
    state: str  # 'inside' | 'outside' | 'unknown'
    confirming: int


def evaluate(
    prev: FenceState, obs: FenceObs, *, confirm: int = CONFIRM_FIXES
) -> tuple[FenceState, str | None]:
    """The pure hysteresis state machine: (next state, 'enter'|'exit'|None)."""
    if prev.state == "inside":
        if obs.inside_buffered:
            return FenceState("inside", 0), None
        count = prev.confirming + 1
        if count >= confirm:
            return FenceState("outside", 0), "exit"
        return FenceState("inside", count), None
    # outside or unknown
    if obs.inside:
        count = prev.confirming + 1
        if count >= confirm:
            return FenceState("inside", 0), "enter"
        return FenceState(prev.state, count), None
    return FenceState("outside", 0), None


_FENCES_SQL = text(
    "WITH pt AS (SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS g)"
    " SELECT pg.id::text AS pgid, pg.place_entity_id::text AS eid,"
    "   CASE WHEN pg.polygon IS NOT NULL THEN ST_Covers(pg.polygon, pt.g)"
    "        ELSE ST_DWithin(pg.center, pt.g, pg.radius_m) END AS inside,"
    "   CASE WHEN pg.polygon IS NOT NULL THEN ST_DWithin(pg.polygon, pt.g, :buf)"
    "        ELSE ST_DWithin(pg.center, pt.g, pg.radius_m + :buf) END AS inside_buffered"
    " FROM app.place_geofence pg, pt WHERE pg.enabled"
)

_UPSERT_STATE_SQL = text(
    "INSERT INTO app.geofence_state"
    " (subject_id, place_geofence_id, state, confirming_fixes, last_fix_at, since)"
    " VALUES (:sid, :pgid, :state, :conf, :ts, :since)"
    " ON CONFLICT (subject_id, place_geofence_id) DO UPDATE SET"
    "   state = excluded.state, confirming_fixes = excluded.confirming_fixes,"
    "   last_fix_at = excluded.last_fix_at,"
    "   since = COALESCE(excluded.since, app.geofence_state.since), updated_at = now()"
)


async def detect_transitions(
    maker: async_sessionmaker[AsyncSession],
    *,
    principal_id: str,
    subject_id: str,
    captured_at: datetime,
    latitude: float,
    longitude: float,
    accuracy_m: float | None = None,
) -> list[dict]:
    """Update geofence state for this fix and emit a transition event per crossing.
    Returns the transitions (for logging/tests). Best-effort: a detection error
    never propagates to break ingest (the fix is already durably stored)."""
    if accuracy_m is not None and accuracy_m > ACCURACY_GATE_M:
        return []
    try:
        transitions = await _evaluate_and_persist(
            maker,
            principal_id=principal_id,
            subject_id=subject_id,
            captured_at=captured_at,
            latitude=latitude,
            longitude=longitude,
        )
    except Exception as exc:  # noqa: BLE001 - detection must not break a stored fix
        log.warning("geofence.detect_failed", subject_id=subject_id, error=repr(exc))
        return []
    for t in transitions:
        await wf_events.emit_event(
            maker,
            wf_events.SYSTEM_CTX,
            type=GEOFENCE_TRANSITION,
            domain_code="location",
            payload={
                "subject_id": subject_id,
                "place_geofence_id": t["place_geofence_id"],
                "place_entity_id": t["place_entity_id"],
                "transition": t["transition"],
                "captured_at": captured_at.isoformat(),
            },
            principal_id=principal_id,
        )
    return transitions


async def _evaluate_and_persist(
    maker: async_sessionmaker[AsyncSession],
    *,
    principal_id: str,
    subject_id: str,
    captured_at: datetime,
    latitude: float,
    longitude: float,
) -> list[dict]:
    transitions: list[dict] = []
    async with scoped_session(maker, device_context(principal_id, subject_id)) as session:
        fences = (
            await session.execute(
                _FENCES_SQL, {"lat": latitude, "lon": longitude, "buf": EXIT_BUFFER_M}
            )
        ).all()
        if not fences:
            return []
        states = {
            r.pgid: FenceState(r.state, r.confirming_fixes)
            for r in (
                await session.execute(
                    text(
                        "SELECT place_geofence_id::text AS pgid, state, confirming_fixes"
                        " FROM app.geofence_state WHERE subject_id::text = :sid"
                    ),
                    {"sid": subject_id},
                )
            ).all()
        }
        for fence in fences:
            prev = states.get(fence.pgid, FenceState("unknown", 0))
            new, transition = evaluate(prev, FenceObs(fence.inside, fence.inside_buffered))
            await session.execute(
                _UPSERT_STATE_SQL,
                {
                    "sid": subject_id,
                    "pgid": fence.pgid,
                    "state": new.state,
                    "conf": new.confirming,
                    "ts": captured_at,
                    "since": captured_at if transition else None,
                },
            )
            if transition:
                transitions.append(
                    {
                        "place_geofence_id": fence.pgid,
                        "place_entity_id": fence.eid,
                        "transition": transition,
                    }
                )
    return transitions
