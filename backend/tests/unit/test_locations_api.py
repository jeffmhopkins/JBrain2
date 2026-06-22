"""The location API: the owner's read-only Devices / Timeline / Map feeds. Fake
repos on app.state (the real RLS firewall is proven in test_locations_read_pg.py);
these assert owner-only access, the identity+activity merge, field mapping, and the
window passthrough."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.citygeocode import CityHit
from jbrain.config import Settings
from jbrain.devices.repo import DeviceInfo
from jbrain.locations import DeviceActivity, FixPoint, PlaceGeofence, TimelineEntry
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeDeviceRepo

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class FakeLocationRepo:
    def __init__(self) -> None:
        self.activity: dict[str, DeviceActivity] = {}
        self.fix_calls: list[dict] = []
        self.timeline_calls: list[dict] = []

    async def device_activity(self, ctx) -> dict[str, DeviceActivity]:  # noqa: ANN001
        return self.activity

    async def fixes(self, ctx, *, subject_id, since, until, limit):  # noqa: ANN001
        self.fix_calls.append(
            {"subject_id": subject_id, "since": since, "until": until, "limit": limit}
        )
        return [
            FixPoint(
                captured_at=NOW,
                latitude=40.0,
                longitude=-74.0,
                accuracy_m=12.5,
                battery_pct=88,
            )
        ]

    async def timeline(self, ctx, *, since, until, limit):  # noqa: ANN001
        self.timeline_calls.append({"since": since, "until": until, "limit": limit})
        return [
            TimelineEntry(
                occurred_at=NOW,
                subject_id="sub-1",
                transition="exit",
                place_entity_id="ent-1",
                place_name="Office",
            )
        ]

    async def places(self, ctx) -> list[PlaceGeofence]:  # noqa: ANN001
        return [
            PlaceGeofence(
                place_entity_id="ent-1",
                name="Office",
                enabled=True,
                center=(40.0, -74.0),
                radius_m=150.0,
                polygon=None,
            ),
            PlaceGeofence(
                place_entity_id="ent-2",
                name="Yard",
                enabled=True,
                center=None,
                radius_m=None,
                polygon=[(40.0, -74.0), (40.1, -74.0), (40.1, -73.9)],
            ),
        ]


class FakeGeocoder:
    """Stub CityGeocoder: a sync `nearest` returning a fixed CityHit (or None)."""

    def __init__(self, hit: CityHit | None) -> None:
        self.hit = hit
        self.calls: list[tuple[float, float]] = []

    def nearest(self, lat: float, lon: float) -> CityHit | None:
        self.calls.append((lat, lon))
        return self.hit


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def geocoder() -> FakeGeocoder:
    return FakeGeocoder(
        CityHit(name="Townsville", region="New York", country="United States", distance_m=900.0)
    )


@pytest.fixture
def devices() -> FakeDeviceRepo:
    return FakeDeviceRepo()


@pytest.fixture
def locs() -> FakeLocationRepo:
    return FakeLocationRepo()


@pytest.fixture
def client(
    repo: FakeAuthRepo,
    devices: FakeDeviceRepo,
    locs: FakeLocationRepo,
    geocoder: FakeGeocoder,
) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.device_repo = devices
        app.state.location_repo = locs
        app.state.city_geocoder = geocoder
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_location_routes_require_owner(client: TestClient) -> None:
    assert client.get("/api/locations/devices").status_code == 401
    assert client.get("/api/locations/fixes", params={"subject_id": "s"}).status_code == 401
    assert client.get("/api/locations/timeline").status_code == 401


def test_devices_merge_identity_with_activity(
    client: TestClient, repo: FakeAuthRepo, devices: FakeDeviceRepo, locs: FakeLocationRepo
) -> None:
    login(client, repo)
    devices.devices = [
        DeviceInfo(id="d-active", label="Phone", created_at=NOW, revoked=False),
        DeviceInfo(id="d-quiet", label="Tablet", created_at=NOW, revoked=True),
    ]
    locs.activity = {
        "d-active": DeviceActivity(
            subject_id="d-active",
            last_seen=NOW,
            battery_pct=72,
            connection="wifi",
            velocity_mps=10.0,
            fix_count=140,
        )
    }
    data = client.get("/api/locations/devices").json()
    assert len(data) == 2
    active = next(d for d in data if d["id"] == "d-active")
    assert active["label"] == "Phone" and active["revoked"] is False
    assert active["last_seen"] == NOW.isoformat() and active["battery_pct"] == 72
    assert active["connection"] == "wifi" and active["fix_count"] == 140
    assert active["velocity_mps"] == 10.0
    # A device with no fixes yet still appears, with null activity + zero count.
    quiet = next(d for d in data if d["id"] == "d-quiet")
    assert quiet["revoked"] is True and quiet["last_seen"] is None
    assert quiet["battery_pct"] is None and quiet["connection"] is None and quiet["fix_count"] == 0
    assert quiet["velocity_mps"] is None


def test_fixes_passes_subject_and_window(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo
) -> None:
    login(client, repo)
    data = client.get(
        "/api/locations/fixes",
        params={
            "subject_id": "sub-1",
            "since": "2026-05-01T00:00:00+00:00",
            "until": "2026-05-02T00:00:00+00:00",
        },
    ).json()
    assert data[0]["latitude"] == 40.0 and data[0]["accuracy_m"] == 12.5
    call = locs.fix_calls[-1]
    assert call["subject_id"] == "sub-1"
    assert call["since"] == datetime(2026, 5, 1, tzinfo=UTC)
    assert call["until"] == datetime(2026, 5, 2, tzinfo=UTC)


def test_fixes_default_window_is_last_day(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo
) -> None:
    login(client, repo)
    assert client.get("/api/locations/fixes", params={"subject_id": "sub-1"}).status_code == 200
    call = locs.fix_calls[-1]
    # No explicit window → roughly the last 24h (since ~= until - 1 day).
    assert (call["until"] - call["since"]).days == 1


def test_fixes_rejects_a_malformed_timestamp(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/locations/fixes", params={"subject_id": "s", "since": "nonsense"})
    assert resp.status_code == 400


def test_timeline_maps_fields_and_defaults_window(
    client: TestClient, repo: FakeAuthRepo, locs: FakeLocationRepo
) -> None:
    login(client, repo)
    data = client.get("/api/locations/timeline").json()
    assert data[0]["transition"] == "exit" and data[0]["place_name"] == "Office"
    assert data[0]["place_entity_id"] == "ent-1" and data[0]["subject_id"] == "sub-1"
    call = locs.timeline_calls[-1]
    assert (call["until"] - call["since"]).days == 30


def test_places_returns_circle_and_polygon_geometry(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    data = client.get("/api/locations/places").json()
    circle = next(p for p in data if p["place_entity_id"] == "ent-1")
    assert circle["center"] == {"lat": 40.0, "lon": -74.0} and circle["radius_m"] == 150.0
    assert circle["polygon"] is None
    poly = next(p for p in data if p["place_entity_id"] == "ent-2")
    assert poly["center"] is None and poly["radius_m"] is None
    assert poly["polygon"][0] == {"lat": 40.0, "lon": -74.0} and len(poly["polygon"]) == 3


def test_places_requires_owner(client: TestClient) -> None:
    assert client.get("/api/locations/places").status_code == 401


def test_reverse_geocode_returns_the_address(
    client: TestClient, repo: FakeAuthRepo, geocoder: FakeGeocoder
) -> None:
    login(client, repo)
    body = client.get("/api/locations/geocode", params={"lat": 40.0, "lon": -74.0}).json()
    assert body == {"address": "Townsville, New York, United States"}
    assert geocoder.calls == [(40.0, -74.0)]


def test_reverse_geocode_fails_closed_to_null(
    client: TestClient, repo: FakeAuthRepo, geocoder: FakeGeocoder
) -> None:
    login(client, repo)
    geocoder.hit = None  # no nearby place → no caption, not a 500
    body = client.get("/api/locations/geocode", params={"lat": 1.0, "lon": 2.0}).json()
    assert body == {"address": None}


def test_geocode_requires_owner(client: TestClient) -> None:
    assert client.get("/api/locations/geocode", params={"lat": 0, "lon": 0}).status_code == 401
