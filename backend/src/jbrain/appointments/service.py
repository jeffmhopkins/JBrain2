"""Appointments — a typed projection of appointment entities (notes are the sole
source of truth, #7). The repository runs every query on an RLS-scoped session,
so domain isolation is Postgres', not these methods' (same pattern as lists).

The agent never writes here directly; the projector upserts a row per entity and
the read tools / ICS feed read it. `AppointmentSpec` is the projector's input,
`AppointmentInfo` the read shape."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from jbrain.db.session import SessionContext

# The appointment.yaml Lifecycle.status enum (mirrored by the DB CHECK).
STATUSES = frozenset(("tentative", "confirmed", "cancelled", "occurred"))


class UnknownDomain(Exception):
    """An appointment was projected into a domain code that doesn't exist."""


@dataclass(frozen=True)
class AppointmentSpec:
    """The projector's upsert input: one appointment entity's current row state.
    The venue is NOT here — it projects into the domain-scoped sidecar separately
    (see AppointmentLocationSpec)."""

    entity_id: str
    domain: str
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    all_day: bool = False
    status: str = "confirmed"
    rrule: str | None = None
    organizer: str | None = None
    attendance_mode: str | None = None
    online_url: str | None = None
    description: str | None = None
    appointment_type: str | None = None
    attendees: list[dict[str, Any]] = field(default_factory=list)
    source_note_id: str | None = None


@dataclass(frozen=True)
class AppointmentLocationSpec:
    """The venue sidecar's upsert input: the venue string in its OWN domain (the
    location fact's domain, typically `location`)."""

    entity_id: str
    domain: str
    location: str
    source_note_id: str | None = None


@dataclass(frozen=True)
class AppointmentInfo:
    id: str
    domain: str
    entity_id: str
    title: str
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool
    status: str
    rrule: str | None
    attendees: list[dict[str, Any]]
    source_note_id: str | None
    created_at: datetime
    updated_at: datetime
    # The where/who facets (general-domain, off the row). Default-None so a read
    # of an older/sparse row needn't name them.
    organizer: str | None = None
    attendance_mode: str | None = None
    online_url: str | None = None
    description: str | None = None
    appointment_type: str | None = None
    # The venue, present only when the reader's session holds its domain (the
    # sidecar's RLS decides); None when there is none or it is out of scope.
    location: str | None = None

    @property
    def recurring(self) -> bool:
        return self.rrule is not None

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"


class AppointmentsRepo(Protocol):
    async def upsert(self, ctx: SessionContext, spec: AppointmentSpec) -> AppointmentInfo:
        """Insert or update the projection row for `spec.entity_id`; raises
        UnknownDomain for a bad domain code."""
        ...

    async def list_appointments(
        self,
        ctx: SessionContext,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        include_cancelled: bool = False,
    ) -> list[AppointmentInfo]:
        """In-scope appointments in `[since, until)`, soonest first."""
        ...

    async def get_appointment(self, ctx: SessionContext, appt_id: str) -> AppointmentInfo | None:
        """One appointment by id; None when missing or out of scope."""
        ...

    async def get_by_entity(self, ctx: SessionContext, entity_id: str) -> AppointmentInfo | None:
        """The projection row for an entity; None when none exists in scope."""
        ...

    async def set_status(
        self, ctx: SessionContext, appt_id: str, status: str
    ) -> AppointmentInfo | None:
        """Move an appointment's lifecycle status; None when missing/out of scope."""
        ...

    async def delete(self, ctx: SessionContext, appt_id: str) -> bool:
        """Delete a projection row; False when missing or out of scope."""
        ...
