"""Project appointment entities into the typed `appointments` read-model.

Appointments are not a source of truth — they are a denormalized view of the
appointment entities the extractor writes (notes are the sole sources of truth,
#7). After a note's facts settle (or a note is purged), this re-derives every
touched appointment entity's CURRENT state from its active facts and upserts one
row per entity — or deletes the row when the entity no longer has a live
scheduled time. It runs inside the caller's transaction on the owner-scoped
session (the pipeline's SYSTEM_CTX, or the owner deleting a note), so it is
atomic with the write that touched the graph and idempotent on re-analysis.

Projected onto the row (all same-domain as the appointment, so the row's own RLS
gates them): title, start/end, lifecycle status, recurrence RRULE, plus the
where/who facets — organizer, attendance mode, online URL, description, type, and
attendees (name + optional ICS params).

The venue is the one field that can't ride the row: a `location`/`address` fact
floors into the location domain (facets.yaml `Located` 🔒), and copying it onto a
general-domain row would leak whereabouts to a non-location session. It projects
into `app.appointment_locations` under its OWN domain, where the same owner+domain
RLS gates it independently (docs/ANALYSIS.md "Mixed-domain notes [split]").
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jbrain.appointments.service import STATUSES
from jbrain.models.analysis import Entity, Fact, TemporalToken
from jbrain.models.appointments import Appointment, AppointmentLocation

# The entity kinds that count as a calendar appointment. The extractor is told
# to use "appointment" (appointment.yaml's type id), but its kinds are free text
# and schema.org leans "Event" — so accept both, case-insensitively. The active
# scheduledTime requirement below is the real gate, so a non-scheduled "event"
# entity never projects, and a person/place (e.g. a misplaced Me.scheduledTime)
# is excluded by kind.
_APPOINTMENT_KINDS = frozenset({"appointment", "event"})
_SCHEDULED_TIME = "scheduledTime"
_RECURRENCE = "recurrence"
_STATUS = "status"
_DEFAULT_STATUS = "confirmed"
_CANCELLED = "cancelled"
_ORGANIZER = "organizer"
_ATTENDEE = "attendee"
_ATTENDANCE_MODE = "attendanceMode"
_ONLINE_URL = "onlineUrl"
_DESCRIPTION = "description"
_APPOINTMENT_TYPE = "appointmentType"
# The venue: a `location` ref (→ a place entity) is preferred; a structured
# `address` on the appointment itself is the fallback. Both floor to the location
# domain, so both route to the sidecar.
_LOCATION = "location"
_ADDRESS = "address"
# A single attendee dict caps these keys — the ICS ATTENDEE params we carry.
_ATTENDEE_PARAMS = ("role", "status", "required")
# A day/month/year-precision schedule is an all-day event (no meaningful clock
# time); only an instant precision is a timed slot.
_ALL_DAY_PRECISIONS = frozenset({"day", "month", "year"})


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


# in-person / online / hybrid, accepting the model's likely drift spellings.
_MODE_ALIASES = {
    "in_person": "in_person",
    "inperson": "in_person",
    "offline": "in_person",
    "physical": "in_person",
    "person": "in_person",
    "online": "online",
    "virtual": "online",
    "remote": "online",
    "hybrid": "hybrid",
    "mixed": "hybrid",
    "both": "hybrid",
}
_ADDRESS_PARTS = (
    "streetAddress",
    "addressLocality",
    "addressRegion",
    "postalCode",
    "addressCountry",
)


def _text_value(value: Any) -> str | None:
    """A plain string out of a scalar/text/enum fact's value_json — unwrapping the
    common {value|text|label|name|url|code: …} envelope, or a bare string/number."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("value", "text", "label", "name", "url", "code"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _coerce_mode(value: str | None) -> str | None:
    """Normalize an attendanceMode value to in_person/online/hybrid; an unknown
    spelling is kept lowercased (the schema's enum is open)."""
    if value is None:
        return None
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    return _MODE_ALIASES.get(key, key) or None


def _format_address(value: Any) -> str | None:
    """A one-line venue string from a structured postal_address (or a bare
    string). Joins the present address parts in postal order."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        parts = [value.get(part) for part in _ADDRESS_PARTS]
        joined = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
        return joined or _text_value(value)
    return None


async def project_appointments(session: AsyncSession, entity_ids: set[uuid.UUID]) -> None:
    """Re-derive and upsert (or remove) the projection for each entity that is an
    appointment. Non-appointment, already-deleted, and timeless entities are
    no-ops / deletions, so it is safe to pass the whole touched/purged set."""
    if not entity_ids:
        return
    # One batched query fetches just the appointment entities up front, so the
    # common case — a note touching only non-appointment entities — costs a single
    # empty SELECT instead of a round-trip per touched entity on the ingest path,
    # and _project_one needs no second lookup or kind re-check.
    ents = (
        await session.execute(
            select(Entity).where(
                Entity.id.in_(entity_ids), func.lower(Entity.kind).in_(_APPOINTMENT_KINDS)
            )
        )
    ).scalars()
    for ent in ents:
        await _project_one(session, ent)


async def _project_one(session: AsyncSession, ent: Entity) -> None:
    eid = ent.id

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

    status = await _current_status(session, eid)
    if sched is None:
        # No live scheduled time. A CANCELLED appointment keeps its existing row
        # (marked cancelled) so the feed still emits STATUS:CANCELLED and a
        # subscribed calendar removes it; anything else timeless (a note edit
        # dropped it, a purge) is removed outright — venue sidecar with it.
        if status == _CANCELLED:
            await session.execute(
                update(Appointment)
                .where(Appointment.entity_id == eid)
                .values(status=_CANCELLED, updated_at=func.now())
            )
        else:
            await _remove(session, eid)
        return
    fact, token_rrule = sched
    value = fact.value_json or {}
    starts_at = _parse_dt(value.get("start")) or fact.valid_from
    ends_at = _parse_dt(value.get("end")) or fact.valid_to
    if starts_at is None:
        await _remove(session, eid)
        return

    # The where/who facets ride the row (all same-domain as the appointment); the
    # venue is split out to its own domain (see _project_location).
    facets = await _load_facets(session, eid)
    names = await _resolve_names(session, facets)

    # Recurrence is a SEPARATE `recurrence` predicate fact binding its own token
    # (facets.yaml Recurrence facet), so the RRULE rarely rides the scheduledTime
    # token — read the recurrence fact's token and prefer whichever carries it.
    rrule = token_rrule or await _recurrence_rrule(session, eid)
    values = {
        "domain_code": ent.domain_code,
        "entity_id": eid,
        "title": ent.canonical_name,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "all_day": fact.temporal_precision in _ALL_DAY_PRECISIONS,
        "status": status,
        "rrule": rrule,
        "source_note_id": fact.note_id,
        **_row_facets(facets, names),
    }
    # One row per entity. On re-projection update the derived fields in place.
    stmt = pg_insert(Appointment).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Appointment.entity_id],
            set_={
                k: stmt.excluded[k]
                for k in (
                    "domain_code",
                    "title",
                    "starts_at",
                    "ends_at",
                    "all_day",
                    "status",
                    "rrule",
                    "source_note_id",
                    "organizer",
                    "attendance_mode",
                    "online_url",
                    "description",
                    "appointment_type",
                    "attendees",
                )
            }
            | {"updated_at": func.now()},
        )
    )
    await _project_location(session, ent, facets, names)


# Predicates whose CURRENT value the projection reads off the row in one fetch.
_FACET_PREDICATES = frozenset(
    {
        _ORGANIZER,
        _ATTENDEE,
        _ATTENDANCE_MODE,
        _ONLINE_URL,
        _DESCRIPTION,
        _APPOINTMENT_TYPE,
        _LOCATION,
        _ADDRESS,
    }
)


async def _load_facets(session: AsyncSession, eid: uuid.UUID) -> dict[str, list[Fact]]:
    """The entity's active facet facts, grouped by predicate, newest validity
    first — single-valued predicates read [0], `attendee` reads them all."""
    rows = (
        await session.execute(
            select(Fact)
            .where(
                Fact.entity_id == eid,
                Fact.status == "active",
                Fact.valid_to.is_(None),  # current facet only; a closed one is history
                Fact.predicate.in_(_FACET_PREDICATES),
            )
            .order_by(Fact.valid_from.desc().nullslast())
        )
    ).scalars()
    grouped: dict[str, list[Fact]] = {}
    for fact in rows:
        grouped.setdefault(fact.predicate, []).append(fact)
    return grouped


async def _resolve_names(
    session: AsyncSession, facets: dict[str, list[Fact]]
) -> dict[uuid.UUID, str]:
    """Canonical names for every entity a facet fact points at (organizer,
    attendees, the location place) — one lookup, so refs render as names."""
    ref_ids = {
        f.object_entity_id
        for pred in (_ORGANIZER, _ATTENDEE, _LOCATION)
        for f in facets.get(pred, [])
        if f.object_entity_id is not None
    }
    if not ref_ids:
        return {}
    rows = await session.execute(
        select(Entity.id, Entity.canonical_name).where(Entity.id.in_(ref_ids))
    )
    return {rid: name for rid, name in rows}


def _ref_label(fact: Fact, names: dict[uuid.UUID, str]) -> str | None:
    """A ref fact as a display string: the pointed-to entity's name, else a name
    carried inline on the fact's value_json (an unresolved mention)."""
    if fact.object_entity_id is not None and fact.object_entity_id in names:
        return names[fact.object_entity_id]
    return _text_value(fact.value_json)


def _row_facets(facets: dict[str, list[Fact]], names: dict[uuid.UUID, str]) -> dict[str, Any]:
    """The general-domain facet columns for the appointment row."""
    organizer = facets.get(_ORGANIZER)
    return {
        "organizer": _ref_label(organizer[0], names) if organizer else None,
        "attendance_mode": _coerce_mode(_text_value(_first_value(facets, _ATTENDANCE_MODE))),
        "online_url": _text_value(_first_value(facets, _ONLINE_URL)),
        "description": _text_value(_first_value(facets, _DESCRIPTION)),
        "appointment_type": _text_value(_first_value(facets, _APPOINTMENT_TYPE)),
        "attendees": _attendees(facets.get(_ATTENDEE, []), names),
    }


def _first_value(facets: dict[str, list[Fact]], predicate: str) -> Any:
    facts = facets.get(predicate)
    return facts[0].value_json if facts else None


def _attendees(facts: list[Fact], names: dict[uuid.UUID, str]) -> list[dict[str, Any]]:
    """One {name, entity_id?, role?, status?, required?} per attendee — name from
    the referenced person, the ICS params from the fact's value_json when given."""
    out: list[dict[str, Any]] = []
    for fact in facts:
        name = _ref_label(fact, names)
        if not name:
            continue
        attendee: dict[str, Any] = {"name": name}
        if fact.object_entity_id is not None:
            attendee["entity_id"] = str(fact.object_entity_id)
        params = fact.value_json if isinstance(fact.value_json, dict) else {}
        for key in _ATTENDEE_PARAMS:
            val = params.get(key)
            if val is not None:
                attendee[key] = val
        out.append(attendee)
    return out


async def _project_location(
    session: AsyncSession,
    ent: Entity,
    facets: dict[str, list[Fact]],
    names: dict[uuid.UUID, str],
) -> None:
    """Upsert (or clear) the venue sidecar in the location fact's OWN domain. A
    `location` ref to a place wins; a structured `address` on the appointment is
    the fallback. No venue fact ⇒ no sidecar row."""
    place = facets.get(_LOCATION)
    address = facets.get(_ADDRESS)
    if place:
        fact = place[0]
        label = _ref_label(fact, names)
    elif address:
        fact = address[0]
        label = _format_address(fact.value_json)
    else:
        label = None
        fact = None
    if fact is None or not label:
        await session.execute(
            delete(AppointmentLocation).where(AppointmentLocation.entity_id == ent.id)
        )
        return
    stmt = pg_insert(AppointmentLocation).values(
        entity_id=ent.id,
        domain_code=fact.domain_code,
        location=label,
        source_note_id=fact.note_id,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[AppointmentLocation.entity_id],
            set_={
                "domain_code": stmt.excluded.domain_code,
                "location": stmt.excluded.location,
                "source_note_id": stmt.excluded.source_note_id,
                "updated_at": func.now(),
            },
        )
    )


async def _remove(session: AsyncSession, eid: uuid.UUID) -> None:
    """Drop a no-longer-scheduled appointment's row and its venue sidecar (an
    entity purge cascades both; this covers a note edit that drops the time)."""
    await session.execute(delete(Appointment).where(Appointment.entity_id == eid))
    await session.execute(delete(AppointmentLocation).where(AppointmentLocation.entity_id == eid))


async def _recurrence_rrule(session: AsyncSession, eid: uuid.UUID) -> str | None:
    """The RRULE on the entity's active `recurrence` fact's token, if any — the
    schema binds recurrence on its own predicate, separate from scheduledTime."""
    row = (
        await session.execute(
            select(TemporalToken.rrule)
            .join(Fact, Fact.temporal_token_id == TemporalToken.id)
            .where(
                Fact.entity_id == eid,
                Fact.predicate == _RECURRENCE,
                Fact.status == "active",
                TemporalToken.rrule.isnot(None),
            )
            .order_by(Fact.valid_from.desc())
            .limit(1)
        )
    ).first()
    return row[0] if row is not None else None


async def _current_status(session: AsyncSession, eid: uuid.UUID) -> str:
    """The appointment's current lifecycle status from its active `status` fact,
    defaulting to `confirmed` — a scheduled appointment with no explicit status
    is a commitment, not a tentative one."""
    row = (
        await session.execute(
            select(Fact.value_json)
            .where(
                Fact.entity_id == eid,
                Fact.predicate == _STATUS,
                Fact.status == "active",
                Fact.valid_to.is_(None),  # current lifecycle status only
            )
            .order_by(Fact.valid_from.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return _DEFAULT_STATUS
    return _coerce_status(row[0]) or _DEFAULT_STATUS
