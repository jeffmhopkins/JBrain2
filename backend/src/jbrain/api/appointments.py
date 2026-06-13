"""Appointments API: the owner's read-only calendar in the PWA.

Owner-only. Appointments are a projection of the owner's notes (the sole source
of truth, #7), so this surface is READ-only — the in-app calendar reads here, and
changes go through the agent (`manage_appointment` stages a Proposal). RLS scopes
every query. The window defaults to the last year forward; the calendar fetches
once and renders Day/Week/Month/Tasks client-side.
"""

from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, owner_only
from jbrain.api.notes import ctx_for
from jbrain.appointments.repo import SqlAppointmentsRepo
from jbrain.appointments.service import AppointmentInfo

router = APIRouter(prefix="/appointments", dependencies=[Depends(owner_only)])

_DEFAULT_HISTORY = timedelta(days=365)


def get_appointments_repo(request: Request) -> SqlAppointmentsRepo:
    return cast(SqlAppointmentsRepo, request.app.state.appointments_repo)


class AppointmentOut(BaseModel):
    id: str
    title: str
    domain: str
    start: str
    end: str | None
    all_day: bool
    status: str
    location: str | None
    rrule: str | None
    recurring: bool
    attendees: list[str]

    @classmethod
    def of(cls, a: AppointmentInfo) -> "AppointmentOut":
        return cls(
            id=a.id,
            title=a.title,
            domain=a.domain,
            start=a.starts_at.isoformat(),
            end=a.ends_at.isoformat() if a.ends_at is not None else None,
            all_day=a.all_day,
            status=a.status,
            location=a.location,
            rrule=a.rrule,
            recurring=a.recurring,
            attendees=[str(p.get("name", "")) for p in a.attendees if p.get("name")],
        )


def _parse(when: str | None) -> datetime | None:
    if not when:
        return None
    try:
        return datetime.fromisoformat(when)
    except ValueError:
        return None


@router.get("")
async def list_appointments(
    request: Request,
    principal: PrincipalDep,
    since: str | None = None,
    until: str | None = None,
    include_cancelled: bool = True,
) -> list[AppointmentOut]:
    """In-scope appointments, soonest first. Defaults to the last year forward;
    cancelled are included so the calendar can show them struck through."""
    repo = get_appointments_repo(request)
    start = _parse(since) or (datetime.now(UTC) - _DEFAULT_HISTORY)
    rows = await repo.list_appointments(
        ctx_for(principal), since=start, until=_parse(until), include_cancelled=include_cancelled
    )
    return [AppointmentOut.of(r) for r in rows]
