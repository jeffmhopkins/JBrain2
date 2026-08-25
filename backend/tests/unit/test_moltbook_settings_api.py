"""The /api/settings/moltbook surface — owner-only registration + the autonomy/kill
switches, with Moltbook's HTTP faked (no network). The bearer key is stored but NEVER
echoed back; the register response carries only non-secret claim material (M17)."""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.web.moltbook import MoltbookClient
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


def _wire_client(app: FastAPI, store: FakeSettingsStore, *, register_status: int = 200) -> None:
    """A MoltbookClient whose HTTP is faked and whose live key comes from the same
    FakeSettingsStore the routes write."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/register"):
            if register_status != 200:
                return httpx.Response(register_status, json={"error": "nope"})
            return httpx.Response(
                200,
                json={
                    "api_key": "moltbook_freshsecret123456",
                    "claim_url": "https://www.moltbook.com/claim/xyz",
                    "verification_code": "reef-9999",
                },
            )
        if request.url.path.endswith("/agents/status"):
            return httpx.Response(200, json={"status": "pending_claim"})
        return httpx.Response(200, json={})

    async def provider() -> tuple[str, str]:
        return await store.moltbook_api_key(None), await store.moltbook_handle(None)

    app.state.moltbook_client = MoltbookClient(provider, transport=httpx.MockTransport(handle))


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FastAPI, FakeSettingsStore]]:
    app = create_app(_settings())
    with TestClient(app) as test_client:
        app.state.auth_repo = FakeAuthRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            test_client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        store = FakeSettingsStore()
        app.state.settings_store = store
        _wire_client(app, store)
        yield test_client, app, store


def test_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/moltbook").status_code == 401


def test_starts_unregistered_switch_off(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, _ = client
    body = test_client.get("/api/settings/moltbook").json()
    assert body["key_set"] is False
    assert body["handle"] == ""
    assert body["autonomy"] is False  # launch OFF (M7)
    assert body["killed"] is False
    assert "experiment" in body["disclosure"]
    assert body["account_state"] == "ok"  # healthy until the integrity watch says otherwise
    assert body["verify_fail_streak"] == 0
    assert body["night_enabled"] is True  # nightly run on by default
    assert body["night_hour"] == 3  # 03:00 owner-local default


def test_nightly_schedule_toggle_and_hour(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    body = test_client.put("/api/settings/moltbook", json={"night_enabled": False}).json()
    assert body["night_enabled"] is False and store.values["moltbook_night_enabled"] is False
    body = test_client.put("/api/settings/moltbook", json={"night_hour": 22}).json()
    assert body["night_hour"] == 22 and store.values["moltbook_night_hour"] == 22
    # An out-of-range hour is rejected by validation (0–23).
    assert test_client.put("/api/settings/moltbook", json={"night_hour": 24}).status_code == 422


def test_register_stores_key_and_returns_only_claim_material(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    resp = test_client.post(
        "/api/settings/moltbook/register", json={"name": "jmolt", "description": "an experiment"}
    )
    body = resp.json()
    # The key is stored + handle set, but the secret is in NO field of the response.
    assert store.values["moltbook_api_key"] == "moltbook_freshsecret123456"
    assert store.values["moltbook_handle"] == "jmolt"
    assert "moltbook_freshsecret123456" not in resp.text
    assert body["claim_url"].endswith("/claim/xyz")
    assert body["verification_code"] == "reef-9999"
    assert "api_key" not in body
    # A follow-up status shows registered without echoing the key.
    status = test_client.get("/api/settings/moltbook").json()
    assert status["key_set"] is True and status["handle"] == "jmolt"


def test_register_failure_maps_to_400(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, app, store = client
    _wire_client(app, store, register_status=429)
    resp = test_client.post("/api/settings/moltbook/register", json={"name": "jmolt"})
    assert resp.status_code == 400


def test_autonomy_switch_and_kill_toggle(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    body = test_client.put("/api/settings/moltbook", json={"autonomy": True}).json()
    assert body["autonomy"] is True and store.values["moltbook_autonomy"] is True
    body = test_client.put("/api/settings/moltbook", json={"killed": True}).json()
    assert body["killed"] is True and store.values["moltbook_killed"] is True


def test_clear_key_disconnects_account(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    test_client.post("/api/settings/moltbook/register", json={"name": "jmolt"})
    body = test_client.put("/api/settings/moltbook", json={"clear_key": True}).json()
    assert body["key_set"] is False and body["handle"] == ""
    assert store.values["moltbook_api_key"] == ""


def test_claim_status_reports_live_state(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, _ = client
    test_client.post("/api/settings/moltbook/register", json={"name": "jmolt"})
    body = test_client.get("/api/settings/moltbook/claim-status").json()
    assert body["status"] == "pending_claim"
