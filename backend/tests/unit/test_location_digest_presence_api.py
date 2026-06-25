"""The L7 digest + presence endpoints: owner-only access, the compute-on-read
orchestration, and coordinate-free output. Fakes on app.state (the real full-owner
RLS barrier over the WEAK tables is proven in the integration suite); these assert a
non-owner is refused before the DB and the response shape carries names + times only.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.locations import Dwell, FixPoint, LatestPlace, NearestFix, PlaceGeofence
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


class FakeLocationRepo:
    def __init__(self) -> None:
        self.dwells_calls: list[dict] = []
        self.dwell_rows: list[Dwell] = []
        self.place_rows: list[PlaceGeofence] = []
        self.near: NearestFix | None = None
        self.place: LatestPlace | None = None
        self.activity: dict = {}

    async def places(self, ctx):  # noqa: ANN001, ANN201
        return self.place_rows

    async def dwells(self, ctx, *, subject_id, since, until, place_entity_id=None):  # noqa: ANN001, ANN201
        self.dwells_calls.append({"subject_id": subject_id, "since": since, "until": until})
        return list(self.dwell_rows)

    async def device_activity(self, ctx):  # noqa: ANN001, ANN201
        return self.activity

    async def nearest_fix(self, ctx, *, subject_id, at, max_gap_seconds):  # noqa: ANN001, ANN201
        return self.near

    async def latest_place(self, ctx, *, subject_id):  # noqa: ANN001, ANN201
        return self.place


class FakeDeviceRepo:
    def __init__(self) -> None:
        self.subs: list[str] = []

    async def owner_device_subjects(self, ctx):  # noqa: ANN001, ANN201
        return self.subs

    async def list(self, ctx):  # noqa: ANN001, ANN201
        return []


class FakeSettingsStore:
    def __init__(self, tz: str | None = "UTC") -> None:
        self._tz = tz

    async def owner_timezone(self, ctx):  # noqa: ANN001, ANN201
        return self._tz


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def locs() -> FakeLocationRepo:
    return FakeLocationRepo()


@pytest.fixture
def devices() -> FakeDeviceRepo:
    return FakeDeviceRepo()


@pytest.fixture
def client(
    repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.location_repo = locs
        app.state.device_repo = devices
        app.state.settings_store = FakeSettingsStore("UTC")
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def _dwell(place: str, entered: datetime, exited: datetime) -> Dwell:
    return Dwell(f"ent-{place}", place, entered, exited, (exited - entered).total_seconds())


# --- digest -----------------------------------------------------------------


def test_digest_requires_owner(client: TestClient) -> None:
    # No session cookie → 401 before any DB/compute (the full-owner gate barrier the
    # WEAK tables depend on; a non-owner never reaches the read).
    assert client.get("/api/locations/digest").status_code == 401
    assert client.get("/api/locations/digest", params={"period": "night"}).status_code == 401


def test_digest_weekly_default_period(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1"]
    locs.place_rows = [
        PlaceGeofence("e-home", "Home", True, (40.0, -74.0), 100.0, None),
    ]
    now = datetime.now(UTC)
    locs.dwell_rows = [_dwell("Home", now - timedelta(hours=10), now - timedelta(hours=2))]
    body = client.get("/api/locations/digest").json()
    assert body["period"] == "week"
    # Default window ~7 days.
    assert (datetime.fromisoformat(body["until"]) - datetime.fromisoformat(body["since"])).days == 7
    assert body["nights_home"] >= 1
    assert "computed_at" in body


def test_digest_nightly_toggle(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1"]
    body = client.get("/api/locations/digest", params={"period": "night"}).json()
    assert body["period"] == "night"
    assert (datetime.fromisoformat(body["until"]) - datetime.fromisoformat(body["since"])).days == 1


def test_digest_rejects_bad_period(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/locations/digest", params={"period": "month"}).status_code == 400


def test_digest_response_is_coordinate_free(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1"]
    locs.place_rows = [PlaceGeofence("e-home", "Home", True, (40.0, -74.0), 100.0, None)]
    now = datetime.now(UTC)
    locs.dwell_rows = [
        _dwell("Office", now - timedelta(hours=20), now - timedelta(hours=12)),
        _dwell("Home", now - timedelta(hours=8), now - timedelta(hours=1)),
    ]
    body = client.get("/api/locations/digest").json()
    # The serialized payload exposes no coordinate field and no fence coordinate value.
    blob = client.get("/api/locations/digest").text
    assert "latitude" not in blob and "longitude" not in blob
    assert "40.0" not in blob and "-74.0" not in blob
    assert body["places_visited"] == 1  # Office; Home is not a visit


def test_digest_merges_dwells_across_owner_devices(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1", "sub-2"]
    client.get("/api/locations/digest")
    # One dwell fetch per owned device subject (merged downstream).
    assert {c["subject_id"] for c in locs.dwells_calls} == {"sub-1", "sub-2"}


# --- presence ---------------------------------------------------------------


def test_presence_requires_owner(client: TestClient) -> None:
    assert client.get("/api/locations/presence").status_code == 401


def test_presence_fresh(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1"]
    # The endpoint computes freshness against real "now", so the fix must be recent.
    real_now = datetime.now(UTC)
    locs.near = NearestFix(
        fix=FixPoint(real_now - timedelta(minutes=4), 40.0, -74.0, 10, 80), gap_seconds=240
    )
    locs.place = LatestPlace("e-home", "Home", real_now)
    body = client.get("/api/locations/presence").json()
    assert body["present"] is True and body["place_name"] == "Home"
    assert body["stale"] is False


def test_presence_stale(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1"]
    real_now = datetime.now(UTC)
    locs.near = NearestFix(
        fix=FixPoint(real_now - timedelta(hours=3), 40.0, -74.0, 10, 80), gap_seconds=3 * 3600
    )
    locs.place = LatestPlace("e-office", "Office", real_now)
    body = client.get("/api/locations/presence").json()
    assert body["present"] is True and body["stale"] is True


def test_presence_absent_when_no_fix(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1"]
    locs.near = None
    body = client.get("/api/locations/presence").json()
    assert body["present"] is False and body["place_name"] is None


def test_presence_is_coordinate_free(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo, devices: FakeDeviceRepo
) -> None:
    login(client, repo)
    devices.subs = ["sub-1"]
    real_now = datetime.now(UTC)
    locs.near = NearestFix(
        fix=FixPoint(real_now - timedelta(minutes=2), 40.123456, -74.654321, 10, 80),
        gap_seconds=120,
    )
    locs.place = LatestPlace("e-home", "Home", real_now)
    blob = client.get("/api/locations/presence").text
    assert "40.123456" not in blob and "-74.654321" not in blob
    assert "latitude" not in blob and "longitude" not in blob
