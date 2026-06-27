"""The jcode api surface: owner-gating of the routes and the stop/restart proxy
(no DB — the gating decisions land before any query; DB writes are neutralized)."""

from __future__ import annotations

import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode
from jbrain.api.deps import current_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.jcode import FakeJcodeClient

OWNER = PrincipalInfo(id="owner1", kind="owner", label="owner")
NON_OWNER = PrincipalInfo(id="cap1", kind="capability_token", label="token")


def _app(principal: PrincipalInfo, *, jcode_client: object) -> FastAPI:
    app = FastAPI()
    app.include_router(jcode.router, prefix="/api")
    app.state.jcode_client = jcode_client
    app.state.session_maker = None  # not reached by the gating tests below
    app.dependency_overrides[current_principal] = lambda: principal
    return app


@pytest.fixture
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the owner-index DB write so a route's happy path runs without a pool."""

    @contextlib.asynccontextmanager
    async def _fake_scoped(*_a: object, **_k: object):
        yield None

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(jcode, "scoped_session", _fake_scoped)
    monkeypatch.setattr(jcode._REPO, "touch", _noop)
    monkeypatch.setattr(jcode._REPO, "delete", _noop)


def test_non_owner_is_forbidden() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.post("/api/jcode/sessions", json={}).status_code == 403
    assert client.get("/api/jcode/sessions").status_code == 403
    # The launcher management routes are owner-only too.
    assert client.patch("/api/jcode/sessions/s1", json={"title": "x"}).status_code == 403
    assert client.post("/api/jcode/sessions/s1/archive").status_code == 403
    assert client.post("/api/jcode/sessions/s1/unarchive").status_code == 403
    assert client.post("/api/jcode/sessions/s1/stop").status_code == 403
    assert client.post("/api/jcode/sessions/s1/restart").status_code == 403


def test_owner_but_unconfigured_is_404() -> None:
    client = TestClient(_app(OWNER, jcode_client=None))
    r = client.post("/api/jcode/sessions", json={"repo": "r"})
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]


def test_stop_and_restart_proxy_the_control_server(_no_db: None) -> None:
    import asyncio

    fake = FakeJcodeClient()
    asyncio.run(fake.create_session("r", "main", ""))  # mints sess1 in the fake
    client = TestClient(_app(OWNER, jcode_client=fake))
    # stop/restart flip the control-server status through the proxy (DB write neutralized).
    assert client.post("/api/jcode/sessions/sess1/stop").json()["status"] == "stopped"
    assert client.post("/api/jcode/sessions/sess1/restart").json()["status"] == "ready"


def test_stop_restart_are_owner_gated() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.post("/api/jcode/sessions/sess1/stop").status_code == 403
    assert client.post("/api/jcode/sessions/sess1/restart").status_code == 403


def test_preview_open_status_close() -> None:
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/sess1/preview").json() == {
        "enabled": True,
        "url": None,
    }
    opened = client.post("/api/jcode/sessions/sess1/preview", json={}).json()
    assert opened["url"].endswith(".trycloudflare.com")
    assert client.delete("/api/jcode/sessions/sess1/preview").status_code == 204


def test_preview_reports_disabled() -> None:
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient(preview_enabled=False)))
    assert client.get("/api/jcode/sessions/sess1/preview").json()["enabled"] is False


def test_preview_is_owner_gated() -> None:
    client = TestClient(_app(NON_OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/sess1/preview").status_code == 403
    assert client.post("/api/jcode/sessions/sess1/preview", json={}).status_code == 403


def test_malformed_sid_is_404_before_any_db_or_control_call() -> None:
    # A sid carrying a path char never reaches the DB (None here) or the control
    # server — _valid_sid 404s first (review S2). session_maker is None, so reaching
    # it would error; a clean 404 proves the guard runs before the body.
    client = TestClient(_app(OWNER, jcode_client=FakeJcodeClient()))
    assert client.get("/api/jcode/sessions/bad.id").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/reset").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/stop").status_code == 404
    assert client.post("/api/jcode/sessions/bad.id/restart").status_code == 404
