"""SQL appointments repository. Every query runs on an RLS-scoped session, so the
owner-only firewall and domain filtering are Postgres', not here. The projector
upserts on `entity_id` (one row per appointment entity); reads back the
materialized row for the tools and the ICS feed."""

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.appointments.service import AppointmentInfo, AppointmentSpec, UnknownDomain
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.appointments import Appointment, AppointmentLocation


def _info(a: Appointment, location: str | None) -> AppointmentInfo:
    return AppointmentInfo(
        id=str(a.id),
        domain=a.domain_code,
        entity_id=str(a.entity_id),
        title=a.title,
        starts_at=a.starts_at,
        ends_at=a.ends_at,
        all_day=a.all_day,
        status=a.status,
        rrule=a.rrule,
        organizer=a.organizer,
        attendance_mode=a.attendance_mode,
        online_url=a.online_url,
        description=a.description,
        appointment_type=a.appointment_type,
        attendees=list(a.attendees or []),
        source_note_id=str(a.source_note_id) if a.source_note_id is not None else None,
        created_at=a.created_at,
        updated_at=a.updated_at,
        # The venue rides in from the LEFT JOIN on the sidecar; RLS already left it
        # NULL when the session can't see that domain.
        location=location,
    )


# The appointment row plus its venue, the venue NULL unless the session holds its
# domain (the sidecar's RLS gates the LEFT JOIN, not this code).
def _with_location():  # type: ignore[no-untyped-def]
    return select(Appointment, AppointmentLocation.location).outerjoin(
        AppointmentLocation, AppointmentLocation.entity_id == Appointment.entity_id
    )


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


class SqlAppointmentsRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def upsert(self, ctx: SessionContext, spec: AppointmentSpec) -> AppointmentInfo:
        eid = _as_uuid(spec.entity_id)
        if eid is None:
            raise ValueError("upsert needs a valid entity_id")
        values = {
            "domain_code": spec.domain,
            "entity_id": eid,
            "title": spec.title,
            "starts_at": spec.starts_at,
            "ends_at": spec.ends_at,
            "all_day": spec.all_day,
            "status": spec.status,
            "rrule": spec.rrule,
            "organizer": spec.organizer,
            "attendance_mode": spec.attendance_mode,
            "online_url": spec.online_url,
            "description": spec.description,
            "appointment_type": spec.appointment_type,
            "attendees": spec.attendees,
            "source_note_id": _as_uuid(spec.source_note_id) if spec.source_note_id else None,
        }
        # One row per entity: a re-projection of the same entity updates in place
        # (a reschedule changes starts_at, a cancellation flips status) without
        # ever duplicating. updated_at bumps; created_at stays.
        stmt = (
            pg_insert(Appointment)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[Appointment.entity_id],
                set_={
                    k: values[k]
                    for k in (
                        "domain_code",
                        "title",
                        "starts_at",
                        "ends_at",
                        "all_day",
                        "status",
                        "rrule",
                        "organizer",
                        "attendance_mode",
                        "online_url",
                        "description",
                        "appointment_type",
                        "attendees",
                        "source_note_id",
                    )
                }
                | {"updated_at": func.now()},
            )
            .returning(Appointment.id)
        )
        try:
            async with scoped_session(self._maker, ctx) as session:
                appt_id = (await session.execute(stmt)).scalar_one()
        except IntegrityError as exc:
            raise UnknownDomain(spec.domain) from exc
        got = await self.get_appointment(ctx, str(appt_id))
        if got is None:  # pragma: no cover - upsert just wrote it in scope
            raise UnknownDomain(spec.domain)
        return got

    async def list_appointments(
        self,
        ctx: SessionContext,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        include_cancelled: bool = False,
    ) -> list[AppointmentInfo]:
        async with scoped_session(self._maker, ctx) as session:
            query = _with_location().order_by(Appointment.starts_at.asc(), Appointment.id.asc())
            if since is not None:
                query = query.where(Appointment.starts_at >= since)
            if until is not None:
                query = query.where(Appointment.starts_at < until)
            if not include_cancelled:
                query = query.where(Appointment.status != "cancelled")
            rows = (await session.execute(query)).all()
            return [_info(a, location) for a, location in rows]

    async def get_appointment(self, ctx: SessionContext, appt_id: str) -> AppointmentInfo | None:
        aid = _as_uuid(appt_id)
        if aid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = (await session.execute(_with_location().where(Appointment.id == aid))).first()
            return _info(row[0], row[1]) if row is not None else None

    async def get_by_entity(self, ctx: SessionContext, entity_id: str) -> AppointmentInfo | None:
        eid = _as_uuid(entity_id)
        if eid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(_with_location().where(Appointment.entity_id == eid))
            ).first()
            return _info(row[0], row[1]) if row is not None else None

    async def set_status(
        self, ctx: SessionContext, appt_id: str, status: str
    ) -> AppointmentInfo | None:
        aid = _as_uuid(appt_id)
        if aid is None:
            return None
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                update(Appointment)
                .where(Appointment.id == aid)
                .values(status=status, updated_at=func.now())
                .returning(Appointment.id)
            )
            if result.scalar_one_or_none() is None:
                return None
        return await self.get_appointment(ctx, appt_id)

    async def delete(self, ctx: SessionContext, appt_id: str) -> bool:
        aid = _as_uuid(appt_id)
        if aid is None:
            return False
        async with scoped_session(self._maker, ctx) as session:
            result = await session.execute(
                delete(Appointment).where(Appointment.id == aid).returning(Appointment.id)
            )
            return result.scalar_one_or_none() is not None
