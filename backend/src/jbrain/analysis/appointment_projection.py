"""Project appointment entities into the typed `appointments` read-model.

Appointments are not a source of truth — they are a denormalized view of the
appointment entities the extractor writes (notes are the sole sources of truth,
#7). After a note's facts settle (or a note is purged), this re-derives every
touched appointment entity's CURRENT state from its active facts and upserts one
row per entity — or deletes the row when the entity no longer has a live
scheduled time. It runs inside the caller's transaction on the owner-scoped
session (the pipeline's SYSTEM_CTX, or the owner deleting a note), so it is
atomic with the write that touched the graph and idempotent on re-analysis.

Scope of this pass (PR2): title, start/end, lifecycle status, and the recurrence
RRULE. Location and attendees are deliberately deferred — an `address` fact
ratchets into the location domain, and copying its value into a general-domain
appointment row would cross the firewall, so located/attendee projection needs
its own domain-aware handling in a later slice.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.appointments.service import STATUSES
from jbrain.models.analysis import Entity, Fact, TemporalToken
from jbrain.models.appointments import Appointment

# The entity `kind` the extractor stamps on an appointment mention (the
# appointment.yaml type id; see the harness appointment scenarios).
APPOINTMENT_KIND = "appointment"
_SCHEDULED_TIME = "scheduledTime"
_STATUS = "status"
_DEFAULT_STATUS = "confirmed"


def _parse_dt(value: Any) -> datetime | None:
    """A start/end out of a fact's value_json (ISO string) or a stored datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _coerce_status(value_json: Any) -> str | None:
    """A Lifecycle `status` fact's value as one of the appointment.yaml enum
    members, or None when it carries nothing recognizable (default applies)."""
    raw: str | None = None
    if isinstance(value_json, str):
        raw = value_json
    elif isinstance(value_json, dict):
        for key in ("value", "status", "code", "label"):
            candidate = value_json.get(key)
            if isinstance(candidate, str):
                raw = candidate
                break
    if raw is not None and raw.lower() in STATUSES:
        return raw.lower()
    return None


async def project_appointments(session: AsyncSession, entity_ids: set[uuid.UUID]) -> None:
    """Re-derive and upsert (or remove) the projection for each entity that is an
    appointment. Non-appointment, already-deleted, and timeless entities are
    no-ops / deletions, so it is safe to pass the whole touched/purged set."""
    for eid in entity_ids:
        await _project_one(session, eid)


async def _project_one(session: AsyncSession, eid: uuid.UUID) -> None:
    ent = (await session.execute(select(Entity).where(Entity.id == eid))).scalar_one_or_none()
    if ent is None or ent.kind != APPOINTMENT_KIND:
        return

    # The current scheduled time is the single ACTIVE scheduledTime state fact —
    # functional, so supersession (validity-newest-wins) already left exactly one.
    sched = (
        await session.execute(
            select(Fact, TemporalToken.rrule)
            .join(TemporalToken, Fact.temporal_token_id == TemporalToken.id, isouter=True)
            .where(
                Fact.entity_id == eid,
                Fact.predicate == _SCHEDULED_TIME,
                Fact.status == "active",
            )
            .order_by(Fact.valid_from.desc())
            .limit(1)
        )
    ).first()

    # No live scheduled time (a note edit dropped it, the fact was purged, or it
    # was never scheduled): there is nothing to put on a calendar, so the
    # projection row goes. A still-orphaned entity was already cascaded away.
    if sched is None:
        await session.execute(delete(Appointment).where(Appointment.entity_id == eid))
        return
    fact, token_rrule = sched
    value = fact.value_json or {}
    starts_at = _parse_dt(value.get("start")) or fact.valid_from
    ends_at = _parse_dt(value.get("end")) or fact.valid_to
    if starts_at is None:
        await session.execute(delete(Appointment).where(Appointment.entity_id == eid))
        return

    status = await _current_status(session, eid)
    values = {
        "domain_code": ent.domain_code,
        "entity_id": eid,
        "title": ent.canonical_name,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "status": status,
        "rrule": token_rrule,
        "source_note_id": fact.note_id,
    }
    # One row per entity. On re-projection update the derived fields in place;
    # location/attendees are intentionally left untouched (this pass does not own
    # them yet — see the module docstring).
    stmt = pg_insert(Appointment).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Appointment.entity_id],
            set_={
                "domain_code": stmt.excluded.domain_code,
                "title": stmt.excluded.title,
                "starts_at": stmt.excluded.starts_at,
                "ends_at": stmt.excluded.ends_at,
                "status": stmt.excluded.status,
                "rrule": stmt.excluded.rrule,
                "source_note_id": stmt.excluded.source_note_id,
                "updated_at": func.now(),
            },
        )
    )


async def _current_status(session: AsyncSession, eid: uuid.UUID) -> str:
    """The appointment's current lifecycle status from its active `status` fact,
    defaulting to `confirmed` — a scheduled appointment with no explicit status
    is a commitment, not a tentative one."""
    row = (
        await session.execute(
            select(Fact.value_json)
            .where(Fact.entity_id == eid, Fact.predicate == _STATUS, Fact.status == "active")
            .order_by(Fact.valid_from.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return _DEFAULT_STATUS
    return _coerce_status(row[0]) or _DEFAULT_STATUS
