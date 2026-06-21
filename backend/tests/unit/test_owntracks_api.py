"""OwnTracks ingest endpoint with a fake location repo + device auth."""

import asyncio
import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import keys
from jbrain.config import Settings
from jbrain.locations import LocationFix
from jbrain.locations.ratelimit import TokenBucket
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

_KEY = "jb1-DEVICE-KEY"


def _basic(key: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"phone:{key}".encode()).decode()}


@dataclass
class FakeLocationRepo:
    calls: list[tuple[str, str, LocationFix]] = field(default_factory=list)
    dup: bool = False

    async def ingest_fix(self, *, principal_id: str, subject_id: str, fix: LocationFix) -> bool:
        self.calls.append((principal_id, subject_id, fix))
        return not self.dup


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeLocationRepo]]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    auth_repo = FakeAuthRepo()
    asyncio.run(auth_repo.create_principal("device_key", keys.hash_key(_KEY), "phone", "subj-1"))
    loc = FakeLocationRepo()
    with TestClient(app) as c:
        app.state.auth_repo = auth_repo
        app.state.location_repo = loc
        app.state.location_rate_limiter = TokenBucket(capacity=100, refill_per_sec=100.0)
        yield c, loc


def _loc(tst: int, **extra: object) -> dict[str, object]:
    return {"_type": "location", "lat": 40.0, "lon": -74.0, "tst": tst, **extra}


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


def test_valid_location_is_ingested_under_the_device_subject(
    client: tuple[TestClient, FakeLocationRepo],
) -> None:
    c, loc = client
    resp = c.post("/api/owntracks", json=_loc(_now(), vel=36.0, batt=80), headers=_basic(_KEY))
    assert resp.status_code == 200
    assert resp.json() == []
    assert len(loc.calls) == 1
    principal_id, subject_id, fix = loc.calls[0]
    assert subject_id == "subj-1"  # code-set from the principal, not the payload
    assert (fix.latitude, fix.longitude) == (40.0, -74.0)
    assert fix.velocity_mps == pytest.approx(10.0)  # 36 km/h -> 10 m/s
    assert fix.battery_pct == 80


def test_non_location_messages_are_acked_and_ignored(
    client: tuple[TestClient, FakeLocationRepo],
) -> None:
    c, loc = client
    for body in ({"_type": "transition", "desc": "home"}, {"_type": "waypoints"}, {"x": 1}):
        assert c.post("/api/owntracks", json=body, headers=_basic(_KEY)).status_code == 200
    assert loc.calls == []


def test_far_future_fix_is_dropped_but_acked(client: tuple[TestClient, FakeLocationRepo]) -> None:
    c, loc = client
    future = int((datetime.now(UTC) + timedelta(days=3)).timestamp())
    assert c.post("/api/owntracks", json=_loc(future), headers=_basic(_KEY)).status_code == 200
    assert loc.calls == []  # bogus clock / spoof: dropped, not stored


def test_schema_invalid_location_is_422(client: tuple[TestClient, FakeLocationRepo]) -> None:
    c, loc = client
    bad = {"_type": "location", "lat": 999, "lon": -74.0, "tst": _now()}
    assert c.post("/api/owntracks", json=bad, headers=_basic(_KEY)).status_code == 422
    assert loc.calls == []


def test_batch_array_ingests_each_under_the_device_subject(
    client: tuple[TestClient, FakeLocationRepo],
) -> None:
    c, loc = client
    now = _now()
    batch = [_loc(now - 2, tid="X"), _loc(now - 1, tid="Y"), _loc(now, batt=55)]
    resp = c.post("/api/owntracks", json=batch, headers=_basic(_KEY))
    assert resp.status_code == 200
    assert resp.json() == []
    assert len(loc.calls) == 3
    # Red-team: every element is stored under the AUTHENTICATED subject, never the
    # payload's `tid` — a batch cannot smuggle another subject's fixes (L9).
    assert {subj for _, subj, _ in loc.calls} == {"subj-1"}
    # Stored oldest-first, in array order.
    assert [int(f.captured_at.timestamp()) for _, _, f in loc.calls] == [now - 2, now - 1, now]


def test_batch_skips_non_location_elements(client: tuple[TestClient, FakeLocationRepo]) -> None:
    c, loc = client
    batch = [_loc(_now()), {"_type": "transition", "desc": "home"}, {"x": 1}]
    assert c.post("/api/owntracks", json=batch, headers=_basic(_KEY)).status_code == 200
    assert len(loc.calls) == 1  # only the one location element is stored


def test_one_invalid_element_rejects_the_whole_batch_without_writing(
    client: tuple[TestClient, FakeLocationRepo],
) -> None:
    c, loc = client
    now = _now()
    batch = [_loc(now - 1), {"_type": "location", "lat": 999, "lon": 0, "tst": now}, _loc(now)]
    assert c.post("/api/owntracks", json=batch, headers=_basic(_KEY)).status_code == 422
    # Whole batch validated before any write: no partial-trust store.
    assert loc.calls == []


def test_batch_over_the_cap_is_422(client: tuple[TestClient, FakeLocationRepo]) -> None:
    c, loc = client
    now = _now()
    batch = [_loc(now - i) for i in range(101)]  # MAX_BATCH is 100
    assert c.post("/api/owntracks", json=batch, headers=_basic(_KEY)).status_code == 422
    assert loc.calls == []


def test_batch_consumes_one_token_per_fix(client: tuple[TestClient, FakeLocationRepo]) -> None:
    c, _ = client
    # Five tokens, no refill: a 3-fix batch then a 3-fix batch — the second overflows.
    cast(FastAPI, c.app).state.location_rate_limiter = TokenBucket(capacity=5, refill_per_sec=0.0)
    now = _now()
    three = [_loc(now - 2), _loc(now - 1), _loc(now)]
    assert c.post("/api/owntracks", json=three, headers=_basic(_KEY)).status_code == 200
    assert c.post("/api/owntracks", json=three, headers=_basic(_KEY)).status_code == 429


def test_requires_device_auth(client: tuple[TestClient, FakeLocationRepo]) -> None:
    c, _ = client
    assert c.post("/api/owntracks", json=_loc(_now())).status_code == 401
    assert c.post("/api/owntracks", json=_loc(_now()), headers=_basic("wrong")).status_code == 401


def test_rate_limited_device_gets_429(client: tuple[TestClient, FakeLocationRepo]) -> None:
    c, _ = client
    cast(FastAPI, c.app).state.location_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0)
    assert c.post("/api/owntracks", json=_loc(_now()), headers=_basic(_KEY)).status_code == 200
    assert c.post("/api/owntracks", json=_loc(_now()), headers=_basic(_KEY)).status_code == 429
