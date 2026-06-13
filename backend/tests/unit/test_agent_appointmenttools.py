"""The agent appointment read tools: formatting, the appointment_card view, the
upcoming-by-default window, and RLS-scope passthrough. The repo is faked (the
real RLS firewall is proven in tests/integration/test_appointments_rls.py)."""

from datetime import UTC, datetime

from jbrain.agent.appointmenttools import (
    build_appointment_handlers,
    build_appointment_write_handlers,
    format_appointment,
    format_appointments,
)
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.appointments.service import AppointmentInfo
from jbrain.db.session import SessionContext

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=("general",))
# A write needs an owner principal on the session (the stage stamps it).
OWNER_CTX = ToolContext(
    session=SessionContext(principal_kind="owner", principal_id="p1"), scopes=("general",)
)
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def appt(
    appt_id: str = "A1",
    title: str = "Dentist",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    status: str = "confirmed",
    rrule: str | None = None,
    location: str | None = None,
    attendees: list[dict] | None = None,
) -> AppointmentInfo:
    return AppointmentInfo(
        id=appt_id,
        domain="general",
        entity_id="e1",
        title=title,
        starts_at=start or datetime(2026, 6, 15, 14, 0, tzinfo=UTC),
        ends_at=end,
        all_day=False,
        location=location,
        status=status,
        rrule=rrule,
        attendees=attendees or [],
        source_note_id="n1",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeAppointments:
    def __init__(
        self, rows: list[AppointmentInfo] | None = None, one: AppointmentInfo | None = None
    ):
        self.rows = rows or []
        self.one = one
        self.calls: list[tuple] = []

    async def list_appointments(self, ctx, *, since=None, until=None, include_cancelled=False):  # noqa: ANN001
        self.calls.append(("list", ctx, since, until, include_cancelled))
        return self.rows

    async def get_appointment(self, ctx, appt_id):  # noqa: ANN001
        self.calls.append(("get", ctx, appt_id))
        return self.one if self.one is not None and appt_id == self.one.id else None


def handlers(fake: FakeAppointments):
    return build_appointment_handlers(fake)  # type: ignore[arg-type]


# --- formatting ----------------------------------------------------------


def test_format_appointments_shows_when_tags_and_id() -> None:
    out = format_appointments(
        [appt(rrule="FREQ=WEEKLY", status="tentative"), appt("A2", "Eye exam")]
    )
    assert "Dentist — 2026-06-15 14:00 [general] (recurring, tentative) id=A1" in out
    assert "Eye exam — 2026-06-15 14:00 [general] id=A2" in out


def test_format_appointments_empty() -> None:
    assert format_appointments([]) == "No appointments in scope."


def test_format_appointments_all_day_shows_date_only() -> None:
    all_day = AppointmentInfo(
        id="A3",
        domain="general",
        entity_id="e3",
        title="Anniversary",
        starts_at=datetime(2026, 7, 4, 0, 0, tzinfo=UTC),
        ends_at=None,
        all_day=True,
        location=None,
        status="confirmed",
        rrule=None,
        attendees=[],
        source_note_id=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert "Anniversary — 2026-07-04 (all day) [general] id=A3" in format_appointments([all_day])


def test_format_appointment_includes_end_location_and_repeat() -> None:
    out = format_appointment(
        appt(
            end=datetime(2026, 6, 15, 15, 0, tzinfo=UTC),
            location="123 Main St",
            rrule="FREQ=WEEKLY",
            attendees=[{"name": "Dr. Nguyen"}],
        )
    )
    assert "when: 2026-06-15 14:00–15:00" in out
    assert "location: 123 Main St" in out
    assert "repeats: FREQ=WEEKLY" in out
    assert "with: Dr. Nguyen" in out


# --- handlers ------------------------------------------------------------


async def test_read_appointments_defaults_to_upcoming_under_scope() -> None:
    fake = FakeAppointments(rows=[appt()])
    out = await handlers(fake)["read_appointments"]({}, CTX)
    assert "Dentist" in out
    kind, session, since, _until, include_cancelled = fake.calls[0]
    assert kind == "list" and session is CTX.session  # ran under the session's scope
    assert include_cancelled is False  # upcoming-only by default
    # Anchored at the START of today (not "now") so earlier-today / all-day events show.
    assert since is not None and (since.hour, since.minute, since.second) == (0, 0, 0)
    assert since.date() == datetime.now(UTC).date()


async def test_read_appointments_include_past_and_cancelled_widen_the_window() -> None:
    fake = FakeAppointments(rows=[])
    out = await handlers(fake)["read_appointments"](
        {"include_past": True, "include_cancelled": True}, CTX
    )
    assert out == "No appointments in scope."
    _kind, _session, since, _until, include_cancelled = fake.calls[0]
    assert since is None and include_cancelled is True


async def test_read_appointment_found_surfaces_a_card_view() -> None:
    out = await handlers(FakeAppointments(one=appt(rrule="FREQ=WEEKLY")))["read_appointment"](
        {"appointment_id": "A1"}, CTX
    )
    assert isinstance(out, ToolOutput)
    assert out.view is not None and out.view.view == "appointment_card"
    assert out.view.data["id"] == "A1"
    assert out.view.data["start"] == "2026-06-15T14:00:00+00:00"
    assert out.view.data["recurring"] is True
    assert out.view.data["status"] == "confirmed"


async def test_read_appointment_missing_and_needs_id() -> None:
    missing = await handlers(FakeAppointments(one=appt()))["read_appointment"](
        {"appointment_id": "other"}, CTX
    )
    assert "in scope" in missing
    needs = await handlers(FakeAppointments())["read_appointment"]({}, CTX)
    assert "needs an appointment_id" in needs


# --- manage_appointment (the write path: stage a Proposal) ----------------


class FakeProposals:
    def __init__(self) -> None:
        self.staged: list = []

    async def stage(self, ctx, *, principal_id, spec):  # noqa: ANN001
        self.staged.append((principal_id, spec))
        return "P1"


def write_handlers(props: FakeProposals, appts: FakeAppointments):
    return build_appointment_write_handlers(props, appts)  # type: ignore[arg-type]


async def test_manage_appointment_create_stages_a_dated_note() -> None:
    props = FakeProposals()
    out = await write_handlers(props, FakeAppointments())["manage_appointment"](
        {"action": "create", "title": "dentist", "when": "next Friday 2pm"}, OWNER_CTX
    )
    assert isinstance(out, ToolOutput)
    assert out.proposal is not None and out.proposal.kind == "appointment"
    principal_id, spec = props.staged[0]
    assert principal_id == "p1" and spec.kind == "appointment"
    node = spec.nodes[0]
    assert node.op == "manage_appointment"
    assert node.preview["body"] == "dentist is scheduled for next Friday 2pm."
    assert node.preview["action"] == "create" and node.preview["domain"] == "general"


async def test_manage_appointment_reschedule_anchors_on_the_existing_appointment() -> None:
    props = FakeProposals()
    existing = FakeAppointments(one=appt("A1", "Dentist with Dr. Nguyen"))
    out = await write_handlers(props, existing)["manage_appointment"](
        {"action": "reschedule", "appointment_id": "A1", "when": "Monday at 3pm"}, OWNER_CTX
    )
    assert out.proposal is not None
    _pid, spec = props.staged[0]
    # Title came from the looked-up appointment, so re-extraction resolves to it.
    assert spec.nodes[0].preview["body"] == (
        "The Dentist with Dr. Nguyen has been rescheduled to Monday at 3pm."
    )
    assert spec.nodes[0].preview["appointment_id"] == "A1"


async def test_manage_appointment_cancel_needs_no_when() -> None:
    props = FakeProposals()
    out = await write_handlers(props, FakeAppointments())["manage_appointment"](
        {"action": "cancel", "title": "gym session"}, OWNER_CTX
    )
    assert out.proposal is not None
    assert props.staged[0][1].nodes[0].preview["body"] == "The gym session has been cancelled."


async def test_manage_appointment_guards() -> None:
    props = FakeProposals()
    h = write_handlers(props, FakeAppointments(one=appt("A1", "x")))["manage_appointment"]
    assert "must be create" in await h({"action": "delete", "title": "x", "when": "now"}, OWNER_CTX)
    assert "needs a 'when'" in await h({"action": "create", "title": "x"}, OWNER_CTX)
    assert "needs a title" in await h({"action": "create", "when": "now"}, OWNER_CTX)
    assert "isn't scoped to it" in await h(
        {"action": "create", "title": "x", "when": "now", "domain": "health"}, OWNER_CTX
    )
    # An unknown appointment_id can't be anchored to.
    assert "in scope" in await h({"action": "cancel", "appointment_id": "missing"}, OWNER_CTX)
    # No owner principal on the session → can't stage.
    assert "without an owner principal" in await h({"action": "cancel", "title": "x"}, CTX)
    # Nothing staged on any guard failure.
    assert props.staged == []
