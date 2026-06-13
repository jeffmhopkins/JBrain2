"""The appointments ICS feed API: the public token-gated feed and the owner-only
token management. A fake settings store + appointments repo on app.state (the
real RLS firewall is proven elsewhere); these assert the auth split and the
token gate."""

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
    ends_at=None,
    all_day=False,
    location=None,
    status="confirmed",
    rrule=None,
    attendees=[],
    source_note_id=None,
    created_at=NOW,
    updated_at=NOW,
)


class FakeSettings:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def get(self, ctx, key, default=None):  # noqa: ANN001
        return self.store.get(key, default)

    async def upsert(self, ctx, key, value):  # noqa: ANN001
        self.store[key] = value


class FakeAppointments:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def list_appointments(self, ctx, *, since=None, until=None, include_cancelled=False):  # noqa: ANN001
        self.calls.append({"include_cancelled": include_cancelled, "since": since})
        return [APPT]


@pytest.fixture
def settings_store() -> FakeSettings:
    return FakeSettings()


@pytest.fixture
def appointments() -> FakeAppointments:
    return FakeAppointments()


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def client(
    repo: FakeAuthRepo, settings_store: FakeSettings, appointments: FakeAppointments
) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.settings_store = settings_store
        app.state.appointments_repo = appointments
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_feed_management_requires_owner(client: TestClient) -> None:
    assert client.get("/api/feed/appointments").status_code == 401
    assert client.post("/api/feed/appointments/rotate").status_code == 401
    assert client.delete("/api/feed/appointments").status_code == 401


def test_rotate_enables_then_serves_the_feed(
    client: TestClient, repo: FakeAuthRepo, appointments: FakeAppointments
) -> None:
    login(client, repo)
    # Disabled until a token is issued.
    assert client.get("/api/feed/appointments").json() == {"enabled": False, "token": None}
    assert client.get("/api/feed/appointments.ics").status_code == 404

    token = client.post("/api/feed/appointments/rotate").json()["token"]
    assert token and client.get("/api/feed/appointments").json() == {
        "enabled": True,
        "token": token,
    }

    # The public feed serves on the right token, with the appointment and the
    # ICS content type — and it pulled cancelled events too (a faithful mirror).
    resp = client.get("/api/feed/appointments.ics", params={"token": token})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    assert "SUMMARY:Dentist" in resp.text.replace("\r\n ", "")
    assert appointments.calls[-1]["include_cancelled"] is True


def test_wrong_or_missing_token_is_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    client.post("/api/feed/appointments/rotate")
    assert client.get("/api/feed/appointments.ics").status_code == 404  # no token
    assert client.get("/api/feed/appointments.ics", params={"token": "nope"}).status_code == 404


def test_rotate_invalidates_the_old_url_and_disable_kills_it(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    login(client, repo)
    old = client.post("/api/feed/appointments/rotate").json()["token"]
    new = client.post("/api/feed/appointments/rotate").json()["token"]
    assert old != new
    assert client.get("/api/feed/appointments.ics", params={"token": old}).status_code == 404
    assert client.get("/api/feed/appointments.ics", params={"token": new}).status_code == 200

    assert client.delete("/api/feed/appointments").status_code == 204
    assert client.get("/api/feed/appointments").json()["enabled"] is False
    assert client.get("/api/feed/appointments.ics", params={"token": new}).status_code == 404
