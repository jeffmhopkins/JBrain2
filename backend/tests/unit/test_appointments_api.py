"""The appointments API: the owner's read-only calendar feed for the PWA. A fake
repo on app.state (the real RLS firewall is proven in test_appointments_rls.py);
these assert owner-only access, the field mapping, and the window passthrough."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.appointments.service import AppointmentInfo
from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

NOW = datetime(2026, 6, 1, tzinfo=UTC)
APPT = AppointmentInfo(
    id="A1",
    domain="health",
    entity_id="e1",
    title="Dentist",
    starts_at=datetime(2026, 7, 1, 14, 0, tzinfo=UTC),
    ends_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC),
    all_day=False,
    location="Maple Dental",
    status="tentative",
    rrule="FREQ=WEEKLY",
    organizer="Maple Dental",
    attendance_mode="in_person",
    online_url="https://meet.example/abc",
    description="Bring insurance card",
    appointment_type="checkup",
    attendees=[
        {"name": "Dr. Nguyen", "entity_id": "p1", "role": "chair", "status": "accepted"},
        {"name": ""},
    ],
    source_note_id="n1",
    created_at=NOW,
    updated_at=NOW,
)


class FakeAppointments:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def list_appointments(self, ctx, *, since=None, until=None, include_cancelled=False):  # noqa: ANN001
        self.calls.append({"since": since, "until": until, "include_cancelled": include_cancelled})
        return [APPT]

    async def get_appointment(self, ctx, appt_id):  # noqa: ANN001
        return APPT if appt_id == APPT.id else None


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def appts() -> FakeAppointments:
    return FakeAppointments()


@pytest.fixture
def client(repo: FakeAuthRepo, appts: FakeAppointments) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.appointments_repo = appts
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_appointments_require_owner(client: TestClient) -> None:
    assert client.get("/api/appointments").status_code == 401


def test_lists_appointments_with_mapped_fields(
    client: TestClient, repo: FakeAuthRepo, appts: FakeAppointments
) -> None:
    login(client, repo)
    data = client.get("/api/appointments").json()
    assert len(data) == 1
    a = data[0]
    assert a["id"] == "A1" and a["title"] == "Dentist" and a["domain"] == "health"
    assert a["start"] == "2026-07-01T14:00:00+00:00" and a["end"] == "2026-07-01T15:00:00+00:00"
    assert a["status"] == "tentative" and a["recurring"] is True and a["rrule"] == "FREQ=WEEKLY"
    assert a["location"] == "Maple Dental"
    # The where/who facets round-trip; attendees carry their ICS params.
    assert a["organizer"] == "Maple Dental" and a["attendance_mode"] == "in_person"
    assert a["online_url"] == "https://meet.example/abc"
    assert a["description"] == "Bring insurance card" and a["appointment_type"] == "checkup"
    assert a["attendees"] == [  # blank names dropped
        {
            "name": "Dr. Nguyen",
            "entity_id": "p1",
            "role": "chair",
            "status": "accepted",
            "required": None,
        }
    ]
    assert a["source_note_id"] == "n1"  # so the calendar can open the source note
    # Defaults: a year-back lower bound, cancelled included for the calendar.
    call = appts.calls[-1]
    assert call["since"] is not None and call["include_cancelled"] is True


def test_window_params_are_passed_through(
    client: TestClient, repo: FakeAuthRepo, appts: FakeAppointments
) -> None:
    login(client, repo)
    client.get(
        "/api/appointments",
        params={
            "since": "2026-07-01T00:00:00+00:00",
            "until": "2026-08-01T00:00:00+00:00",
            "include_cancelled": "false",
        },
    )
    call = appts.calls[-1]
    assert call["since"] == datetime(2026, 7, 1, tzinfo=UTC)
    assert call["until"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert call["include_cancelled"] is False


def test_single_event_ics_download(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/appointments/A1.ics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.text.replace("\r\n ", "")
    assert "BEGIN:VEVENT" in body and "SUMMARY:Dentist" in body
    # A missing/out-of-scope id is a 404, never a leak.
    assert client.get("/api/appointments/nope.ics").status_code == 404


def test_ics_requires_owner(client: TestClient) -> None:
    assert client.get("/api/appointments/A1.ics").status_code == 401


def test_malformed_dates_fall_back_to_defaults(
    client: TestClient, repo: FakeAuthRepo, appts: FakeAppointments
) -> None:
    login(client, repo)
    assert (
        client.get("/api/appointments", params={"since": "nope", "until": "junk"}).status_code
        == 200
    )
    call = appts.calls[-1]
    # A bad `since` falls back to the year-back default; a bad `until` to no bound.
    assert call["since"] is not None and call["until"] is None
