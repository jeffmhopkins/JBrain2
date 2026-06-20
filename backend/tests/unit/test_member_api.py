"""The member dashboard API (JBrain360 M4b): positions + presence, device-cookie
gated, 30-day capped. Fakes on app.state assert the gate (only a member cookie),
the roster/positions mapping, the server-side 30-day clamp, and that a read is
audited. The RLS firewall itself (member sees self + group only) is proven against
real Postgres in test_member_reads_pg.py."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import keys, service
from jbrain.config import Settings
from jbrain.locations import FixPoint, MemberSubject, PlaceGeofence, TimelineEntry
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakePrincipal

DEVICE_KEY = "jb-member-key"
SUBJECT = "subject-self"
NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


class FakeLocationRepo:
    def __init__(self) -> None:
        self.roster = [
            MemberSubject(
                subject_id=SUBJECT,
                label="Me",
                last_seen=NOW,
                battery_pct=80,
                connection="wifi",
            ),
            MemberSubject(
                subject_id="subject-sib",
                label="Sibling",
                last_seen=None,
                battery_pct=None,
                connection=None,
            ),
        ]
        self.fix_calls: list[dict] = []
        self.view_calls: list[dict] = []
        self.timeline_calls: list[dict] = []

    async def member_roster(self, ctx, *, viewer_subject_id):  # noqa: ANN001
        self.viewer = viewer_subject_id
        return self.roster

    async def fixes(self, ctx, *, subject_id, since, until, limit):  # noqa: ANN001
        self.fix_calls.append(
            {"subject_id": subject_id, "since": since, "until": until, "limit": limit}
        )
        return [
            FixPoint(
                captured_at=NOW,
                latitude=40.0,
                longitude=-74.0,
                accuracy_m=10.0,
                battery_pct=80,
            )
        ]

    async def record_view(self, ctx, **kwargs):  # noqa: ANN001, ANN003
        self.view_calls.append(kwargs)

    async def member_places(self, ctx):  # noqa: ANN001
        return [
            PlaceGeofence(
                place_entity_id="ent-home",
                name="Home",
                enabled=True,
                center=(40.0, -74.0),
                radius_m=120.0,
                polygon=None,
            )
        ]

    async def member_timeline(self, ctx, *, viewer_subject_id, since, until, limit):  # noqa: ANN001
        self.timeline_calls.append({"viewer": viewer_subject_id, "since": since, "until": until})
        return [
            TimelineEntry(
                occurred_at=NOW,
                subject_id=SUBJECT,
                transition="enter",
                place_entity_id="ent-home",
                place_name="Home",
            )
        ]


@pytest.fixture
def repo() -> FakeAuthRepo:
    r = FakeAuthRepo()
    r.principals.append(
        FakePrincipal(
            id="dev-1",
            kind="device_key",
            key_hash=keys.hash_key(DEVICE_KEY),
            label="Phone",
            subject_id=SUBJECT,
        )
    )
    return r


@pytest.fixture
def locs() -> FakeLocationRepo:
    return FakeLocationRepo()


@pytest.fixture
def client(repo: FakeAuthRepo, locs: FakeLocationRepo) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.location_repo = locs
        yield test_client


def _as_member(client: TestClient) -> None:
    assert client.post("/api/session/mint", json={"device_key": DEVICE_KEY}).status_code == 204


def test_member_routes_require_a_member_cookie(client: TestClient, repo: FakeAuthRepo) -> None:
    # No cookie at all -> 401.
    assert client.get("/api/member/roster").status_code == 401
    # An owner cookie is authenticated but not a member -> 403.
    owner_key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": owner_key}).status_code == 204
    assert client.get("/api/member/roster").status_code == 403


def test_roster_lists_visible_subjects(client: TestClient, locs: FakeLocationRepo) -> None:
    _as_member(client)
    data = client.get("/api/member/roster").json()
    assert locs.viewer == SUBJECT
    assert [r["subject_id"] for r in data] == [SUBJECT, "subject-sib"]
    me = data[0]
    assert me["label"] == "Me" and me["last_seen"] == NOW.isoformat()
    sib = data[1]
    assert sib["last_seen"] is None and sib["battery_pct"] is None


def test_positions_returns_trail_and_audits(client: TestClient, locs: FakeLocationRepo) -> None:
    _as_member(client)
    data = client.get("/api/member/positions", params={"subject_id": "subject-sib"}).json()
    assert len(data) == 1 and data[0]["latitude"] == 40.0
    assert locs.fix_calls[0]["subject_id"] == "subject-sib"
    # The read recorded a who-saw-whom row attributing the member to itself.
    assert locs.view_calls[0]["target_subject_id"] == "subject-sib"
    assert locs.view_calls[0]["viewer_subject_id"] == SUBJECT
    assert locs.view_calls[0]["path"] == "history"


def test_positions_clamps_to_thirty_day_cap(client: TestClient, locs: FakeLocationRepo) -> None:
    _as_member(client)
    # Ask for a year of history; the server must pull `since` forward to ~30 days.
    client.get(
        "/api/member/positions",
        params={"subject_id": SUBJECT, "since": "2020-01-01T00:00:00+00:00"},
    )
    since = locs.fix_calls[0]["since"]
    floor = datetime.now(UTC) - timedelta(days=30)
    assert since >= floor - timedelta(seconds=5)


def test_positions_audit_failure_does_not_500(client: TestClient, locs: FakeLocationRepo) -> None:
    async def boom(ctx, **kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("audit down")

    locs.record_view = boom  # type: ignore[method-assign]
    _as_member(client)
    resp = client.get("/api/member/positions", params={"subject_id": SUBJECT})
    assert resp.status_code == 200


def test_places_returns_shared_overlay(client: TestClient) -> None:
    _as_member(client)
    data = client.get("/api/member/places").json()
    assert [p["name"] for p in data] == ["Home"]
    assert data[0]["center"] == {"lat": 40.0, "lon": -74.0}


def test_timeline_passes_viewer_and_clamps_cap(client: TestClient, locs: FakeLocationRepo) -> None:
    _as_member(client)
    data = client.get("/api/member/timeline", params={"since": "2020-01-01T00:00:00+00:00"}).json()
    assert data[0]["transition"] == "enter" and data[0]["place_name"] == "Home"
    call = locs.timeline_calls[0]
    assert call["viewer"] == SUBJECT
    floor = datetime.now(UTC) - timedelta(days=30)
    assert call["since"] >= floor - timedelta(seconds=5)  # 30-day cap clamps the year request


def test_member_places_and_timeline_require_member_cookie(client: TestClient) -> None:
    assert client.get("/api/member/places").status_code == 401
    assert client.get("/api/member/timeline").status_code == 401
