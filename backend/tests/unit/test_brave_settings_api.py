"""The /api/settings/brave surface — owner-only toggle, key and monthly budget, this month's usage,
and a live "Test key" probe, with Brave's HTTP faked (no network). The key is stored but NEVER
echoed back."""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.web.search import BraveConfig, BraveSearch, BraveUsage, utc_month
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore

_BRAVE_OK = {"web": {"results": [{"title": "T", "url": "https://t.example/", "description": "d"}]}}


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


def _wire_brave(
    app: FastAPI, store: FakeSettingsStore, settings: Settings, *, status: int = 200
) -> list[httpx.Request]:
    """A BraveSearch whose endpoint is faked and whose live settings, usage and last error all
    go through the same FakeSettingsStore the routes write — so a save is seen by the probe."""
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, json=_BRAVE_OK if status == 200 else {})

    async def provider() -> BraveConfig:
        return BraveConfig(
            await store.brave_enabled(None),
            await store.brave_api_key(None) or settings.brave_api_key,
            await store.brave_monthly_budget(None),
        )

    app.state.brave_search = BraveSearch(
        settings.brave_url,
        provider,
        BraveUsage(lambda: store.brave_usage(None), lambda r: store.set_brave_usage(None, r)),
        httpx.MockTransport(handle),
        save_error=lambda r: store.set_brave_last_error(None, r),
    )
    return sent


def _login(test_client: TestClient, app: FastAPI) -> None:
    app.state.auth_repo = FakeAuthRepo()
    key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
    resp = test_client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
    assert resp.status_code == 204


Box = tuple[TestClient, FastAPI, FakeSettingsStore, list[httpx.Request]]


@pytest.fixture
def client() -> Iterator[Box]:
    settings = _settings()
    app = create_app(settings)
    with TestClient(app) as test_client:
        _login(test_client, app)
        store = FakeSettingsStore()
        app.state.settings_store = store
        sent = _wire_brave(app, store, settings)
        yield test_client, app, store, sent


def test_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/brave").status_code == 401
        assert anon.put("/api/settings/brave", json={"enabled": False}).status_code == 401
        assert anon.post("/api/settings/brave/test").status_code == 401


@pytest.mark.parametrize("kind", ["device_key", "jcode_share_link", "capability_token"])
def test_a_non_owner_is_refused_and_changes_nothing(client: Box, kind: str) -> None:
    test_client, app, store, sent = client
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="other", kind=kind, label="not the owner", jcode_session_id="s1"
    )
    try:
        assert test_client.get("/api/settings/brave").status_code == 403
        put = test_client.put("/api/settings/brave", json={"api_key": "x", "monthly_budget": 5})
        assert put.status_code == 403
        assert test_client.post("/api/settings/brave/test").status_code == 403
    finally:
        app.dependency_overrides.clear()
    assert store.values == {} and sent == []


def test_starts_enabled_keyless_with_the_default_budget(client: Box) -> None:
    test_client, _, _, _ = client
    assert test_client.get("/api/settings/brave").json() == {
        "enabled": True,
        "key_present": False,
        "key_source": "none",
        "wired": True,
        "effective": False,
        "blocked": "",
        "budget": 900,
        "used_this_month": 0,
        "month": utc_month(),
        "last_error": "",
        "last_error_at": "",
    }


def test_saving_a_key_never_echoes_it(client: Box) -> None:
    test_client, _, store, _ = client
    resp = test_client.put("/api/settings/brave", json={"api_key": "  brv-secret  "})
    body = resp.json()
    assert body["key_present"] is True and body["key_source"] == "stored"
    assert body["effective"] is True
    assert "brv-secret" not in resp.text
    assert "brv-secret" not in test_client.get("/api/settings/brave").text
    assert store.values["brave_api_key"] == "brv-secret"


def test_toggle_and_budget_change_without_touching_the_key(client: Box) -> None:
    test_client, _, store, _ = client
    test_client.put("/api/settings/brave", json={"api_key": "brv-secret"})
    body = test_client.put(
        "/api/settings/brave", json={"enabled": False, "monthly_budget": 250}
    ).json()
    assert body["enabled"] is False and body["budget"] == 250 and body["effective"] is False
    assert store.values["brave_api_key"] == "brv-secret"


@pytest.mark.parametrize("budget", [0, -1, 100_001, "lots", 1.5])
def test_an_out_of_range_budget_is_refused(client: Box, budget: object) -> None:
    test_client, _, store, _ = client
    resp = test_client.put("/api/settings/brave", json={"monthly_budget": budget})
    assert resp.status_code == 422 and "brave_monthly_budget" not in store.values


def test_unknown_fields_are_refused(client: Box) -> None:
    test_client, _, _, _ = client
    assert test_client.put("/api/settings/brave", json={"key": "x"}).status_code == 422


def test_an_empty_key_reverts_to_the_env_fallback() -> None:
    settings = _settings(brave_api_key="env-key")
    app = create_app(settings)
    with TestClient(app) as test_client:
        _login(test_client, app)
        store = FakeSettingsStore()
        app.state.settings_store = store
        _wire_brave(app, store, settings)
        body = test_client.get("/api/settings/brave").json()
        assert body["key_present"] is True and body["key_source"] == "env"
        test_client.put("/api/settings/brave", json={"api_key": "brv-stored"})
        assert test_client.get("/api/settings/brave").json()["key_source"] == "stored"
        resp = test_client.put("/api/settings/brave", json={"api_key": ""})
        assert resp.json()["key_source"] == "env" and store.values["brave_api_key"] == ""
        assert "env-key" not in resp.text


def test_usage_and_last_error_are_reported(client: Box) -> None:
    test_client, _, store, _ = client
    store.values["brave_api_key"] = "brv-secret"
    store.values["brave_monthly_budget"] = 10
    store.values["brave_usage"] = {"month": utc_month(), "count": 10}
    store.values["brave_last_error"] = {"detail": "Brave rejected the API key", "at": "t0"}
    body = test_client.get("/api/settings/brave").json()
    assert body["used_this_month"] == 10 and body["effective"] is False  # at budget
    assert body["last_error"] == "Brave rejected the API key" and body["last_error_at"] == "t0"
    store.values["brave_usage"] = {"month": "1999-01", "count": 10}  # last month's spend
    store.values["brave_last_error"] = "junk"
    body = test_client.get("/api/settings/brave").json()
    assert body["used_this_month"] == 0 and body["effective"] is True and body["last_error"] == ""


def test_the_probe_spends_one_counted_query(client: Box) -> None:
    test_client, _, store, sent = client
    test_client.put("/api/settings/brave", json={"api_key": "brv-secret"})
    resp = test_client.post("/api/settings/brave/test")
    assert resp.json() == {
        "ok": True,
        "hits": 1,
        "detail": "Brave answered with 1 result(s) — the key works.",
    }
    assert "brv-secret" not in resp.text
    assert len(sent) == 1 and sent[0].headers["X-Subscription-Token"] == "brv-secret"
    assert test_client.get("/api/settings/brave").json()["used_this_month"] == 1


def test_the_probe_reports_a_rejected_key_and_records_it(client: Box) -> None:
    test_client, app, store, _ = client
    _wire_brave(app, store, _settings(), status=401)
    test_client.put("/api/settings/brave", json={"api_key": "bad-key"})
    body = test_client.post("/api/settings/brave/test").json()
    assert body["ok"] is False and "rejected the API key (HTTP 401)" in body["detail"]
    status = test_client.get("/api/settings/brave").json()
    assert "rejected the API key" in status["last_error"]


def test_a_key_brave_refused_reads_as_blocked_until_a_new_key_is_saved(client: Box) -> None:
    test_client, app, store, _ = client
    settings = _settings()
    sent = _wire_brave(app, store, settings, status=422)
    test_client.put("/api/settings/brave", json={"api_key": "bad-key"})
    test_client.post("/api/settings/brave/test")
    status = test_client.get("/api/settings/brave").json()
    assert status["blocked"] == "key_rejected" and status["effective"] is False
    assert status["last_error"].startswith("Brave rejected the API key (HTTP 422)")
    # Saving a different key clears both the stored error and the learned block.
    status = test_client.put("/api/settings/brave", json={"api_key": "good-key"}).json()
    assert status["blocked"] == "" and status["effective"] is True and status["last_error"] == ""
    assert len(sent) == 1


def test_the_probe_without_a_key_sends_nothing(client: Box) -> None:
    test_client, _, _, sent = client
    body = test_client.post("/api/settings/brave/test").json()
    assert body["ok"] is False and "No Brave API key" in body["detail"] and sent == []
