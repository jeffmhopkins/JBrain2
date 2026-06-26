"""The /api/jcode share-link HTTP surface: owner mint/list/revoke + public redeem.

The owner cookie gates management; redeem exchanges a share secret for a session
cookie scoped to one session. The end-to-end scope enforcement (a redeemed cookie
can't escalate to owner routes) is pinned here against the real app + fake auth repo.
"""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_COOKIE = "jbrain_session"


@pytest.fixture
def app_repo() -> Iterator[tuple[FastAPI, FakeAuthRepo]]:
    app = create_app(Settings(secure_cookies=False, database_url=_DB, session_cookie=_COOKIE))
    repo = FakeAuthRepo()
    with TestClient(app):  # run lifespan once so app.state is wired
        app.state.auth_repo = repo
        yield app, repo


def _owner(app: FastAPI, repo: FakeAuthRepo) -> TestClient:
    client = TestClient(app)
    key = asyncio.run(auth_service.rotate_owner_key(repo))
    assert (
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
    ).status_code == 204
    return client


def test_mint_list_revoke_roundtrip(app_repo: tuple[FastAPI, FakeAuthRepo]) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    minted = owner.post("/api/jcode/sessions/sess-a/share", json={"label": "Sarah", "ttl_hours": 6})
    assert minted.status_code == 201
    body = minted.json()
    assert body["token"] and body["label"] == "Sarah" and body["expires_at"]
    share_id = body["id"]

    listed = owner.get("/api/jcode/sessions/sess-a/shares").json()
    assert [s["id"] for s in listed] == [share_id]
    assert "token" not in listed[0]  # the list never carries secrets

    assert owner.delete(f"/api/jcode/sessions/sess-a/shares/{share_id}").status_code == 204
    assert owner.get("/api/jcode/sessions/sess-a/shares").json() == []
    # A second revoke is a clean 404 (unknown / already-revoked).
    assert owner.delete(f"/api/jcode/sessions/sess-a/shares/{share_id}").status_code == 404


def test_share_management_is_owner_only(app_repo: tuple[FastAPI, FakeAuthRepo]) -> None:
    app, repo = app_repo
    # No owner cookie → mint/list 401 (not authenticated).
    anon = TestClient(app)
    assert anon.post("/api/jcode/sessions/s/share", json={}).status_code == 401
    assert anon.get("/api/jcode/sessions/s/shares").status_code == 401


def test_redeem_scopes_a_cookie_that_cannot_escalate(
    app_repo: tuple[FastAPI, FakeAuthRepo],
) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    token = owner.post("/api/jcode/sessions/sess-a/share", json={"ttl_hours": 6}).json()["token"]

    # A fresh browser (no owner cookie) redeems the secret → gets a scoped session cookie.
    guest = TestClient(app)
    redeemed = guest.post("/api/jcode/share/redeem", json={"token": token})
    assert redeemed.status_code == 200
    assert redeemed.json()["session_id"] == "sess-a"
    assert _COOKIE in guest.cookies

    # That cookie CANNOT reach owner-only management: minting another share or deleting
    # the session both 403 (the share principal is not the owner).
    assert guest.post("/api/jcode/sessions/sess-a/share", json={}).status_code == 403
    assert guest.delete("/api/jcode/sessions/sess-a").status_code == 403


def test_redeem_rejects_a_bad_secret_without_a_cookie(
    app_repo: tuple[FastAPI, FakeAuthRepo],
) -> None:
    app, _ = app_repo
    guest = TestClient(app)
    r = guest.post("/api/jcode/share/redeem", json={"token": "nope"})
    assert r.status_code == 401
    assert _COOKIE not in guest.cookies
