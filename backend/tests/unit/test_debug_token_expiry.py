"""A debug token tells its holder when it dies.

A 24 h token lapsed at 04:00 UTC under an unattended training session, and the first the
session knew of it was a bare "invalid debug token". These pin the three places the
expiry now shows: headers on every authenticated response, the whoami body, and a 401 that
says "expired at" rather than "invalid".
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jbrain.api.debug_expiry import (
    EXPIRES_AT_HEADER,
    EXPIRES_IN_HEADER,
    WARNING_HEADER,
    expiry_headers,
    token_expiry,
)
from jbrain.auth import service as auth_service
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo
from tests.unit.test_debug_api import _auth, _settings

_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _client_with(repo: FakeAuthRepo) -> Iterator[TestClient]:
    app = create_app(_settings())
    with TestClient(app) as client:
        app.state.auth_repo = repo
        yield client


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    yield from _client_with(repo)


def test_a_day_or_more_left_carries_no_warning() -> None:
    e = token_expiry(_NOW + timedelta(days=3), now=_NOW)
    assert e.expires_in_seconds == 3 * 86400 and e.warning is None
    assert WARNING_HEADER not in expiry_headers(e)
    assert expiry_headers(e)[EXPIRES_AT_HEADER] == "2026-11-18T12:00:00Z"


def test_inside_the_last_day_it_warns_with_time_left_and_how_to_renew() -> None:
    e = token_expiry(_NOW + timedelta(hours=5, minutes=12, seconds=30), now=_NOW)
    assert e.warning is not None
    assert "expires in 5h 12m" in e.warning and "2026-11-15T17:12:30Z" in e.warning
    assert "Settings → Debug access" in e.warning
    # Header values must be latin-1 encodable or the response fails to send.
    expiry_headers(e)[WARNING_HEADER].encode("latin-1")


def test_minutes_only_and_never_floors() -> None:
    assert "expires in 7m" in (token_expiry(_NOW + timedelta(minutes=7), now=_NOW).warning or "")
    assert token_expiry(_NOW - timedelta(minutes=1), now=_NOW).expires_in_seconds == 0
    never = token_expiry(None)
    assert never.expires_in_seconds is None and never.warning is None
    assert expiry_headers(never) == {EXPIRES_AT_HEADER: "never"}


def test_every_authenticated_response_carries_the_expiry(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    key, record = asyncio.run(auth_service.mint_capability(repo, "claude", ttl_hours=72))
    r = client.get("/api/debug/whoami", headers=_auth(key))
    assert r.status_code == 200
    assert record.expires_at is not None
    assert datetime.fromisoformat(r.headers[EXPIRES_AT_HEADER]) == record.expires_at.replace(
        microsecond=0
    )
    assert 72 * 3600 - 60 < int(r.headers[EXPIRES_IN_HEADER]) <= 72 * 3600
    assert WARNING_HEADER not in r.headers
    body = r.json()
    assert body["expires_in_seconds"] > 72 * 3600 - 60 and body["expiry_warning"] is None
    assert datetime.fromisoformat(body["expires_at"]) == record.expires_at
    # Not only whoami: the middleware stamps every authenticated debug route.
    assert EXPIRES_AT_HEADER in client.get("/api/debug/version", headers=_auth(key)).headers


def test_a_short_lived_token_warns_in_header_and_body(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    key, _ = asyncio.run(auth_service.mint_capability(repo, "claude", ttl_hours=2))
    r = client.get("/api/debug/whoami", headers=_auth(key))
    assert "expires in 1h 59m" in r.headers[WARNING_HEADER]
    assert "expires in 1h 59m" in r.json()["expiry_warning"]


def test_an_expired_token_401s_saying_when_it_expired(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    key, record = asyncio.run(auth_service.mint_capability(repo, "claude", ttl_hours=-1))
    r = client.get("/api/debug/whoami", headers=_auth(key))
    assert r.status_code == 401
    assert r.json()["detail"].startswith("debug token expired at ")
    assert "Settings → Debug access" in r.json()["detail"]
    assert record.expires_at is not None
    assert datetime.fromisoformat(r.headers[EXPIRES_AT_HEADER]) == record.expires_at.replace(
        microsecond=0
    )


def test_unknown_revoked_and_suspended_keys_stay_plain_invalid(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    # Expiry is the only failure that gets named; the rest stay the generic 401, and none
    # of them gains expiry headers from the middleware.
    revoked, rec = asyncio.run(auth_service.mint_capability(repo, "r", ttl_hours=-1))
    asyncio.run(repo.revoke_capability(rec.id))
    suspended, rec2 = asyncio.run(auth_service.mint_capability(repo, "s", ttl_hours=24))
    asyncio.run(repo.suspend_capability(rec2.id))
    for key in ("garbage", revoked, suspended):
        r = client.get("/api/debug/whoami", headers=_auth(key))
        assert r.status_code == 401 and r.json()["detail"] == "invalid debug token"
        assert EXPIRES_AT_HEADER not in r.headers
