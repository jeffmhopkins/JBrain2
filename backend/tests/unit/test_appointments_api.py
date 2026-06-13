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
    attendees=[{"name": "Dr. Nguyen"}, {"name": ""}],
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
    assert a["attendees"] == ["Dr. Nguyen"]  # blank names dropped
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
