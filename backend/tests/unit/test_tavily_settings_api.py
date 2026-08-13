"""The /api/settings/tavily surface — owner-only toggle + key entry and a live "Test key"
probe, with Tavily's HTTP faked (no network). The key is stored but NEVER echoed back."""

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
from jbrain.web.fetch import WebFetcher
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


_TAVILY_OK = {
    "results": [
        {"url": "https://example.com", "raw_content": "Extracted content proving the key works."}
    ]
}


def _wire_fetcher(app: FastAPI, store: FakeSettingsStore, *, tavily_status: int = 200) -> None:
    """Give the app a WebFetcher whose Tavily endpoint is faked and whose live (enabled, key) come
    from the same FakeSettingsStore the routes write — so a save is visible to the Test probe."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            return httpx.Response(tavily_status, json=_TAVILY_OK if tavily_status == 200 else {})
        return httpx.Response(200, content=b"<html><body>x</body></html>")

    async def provider() -> tuple[bool, str]:
        return await store.tavily_enabled(None), await store.tavily_api_key(None)

    app.state.web_fetcher = WebFetcher(
        transport=httpx.MockTransport(handle),
        tavily_url="https://api.tavily.com",
        tavily_settings=provider,
    )


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
        _wire_fetcher(app, store)
        yield test_client, app, store


def test_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/tavily").status_code == 401


def test_starts_enabled_and_keyless(client: tuple[TestClient, FastAPI, FakeSettingsStore]) -> None:
    test_client, _, _ = client
    body = test_client.get("/api/settings/tavily").json()
    # Default: enabled ON (single-owner box), no key yet, wired but not effective until a key.
    assert body == {"enabled": True, "key_set": False, "wired": True, "effective": False}


def test_saving_a_key_sets_it_without_echoing(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    resp = test_client.put("/api/settings/tavily", json={"api_key": "tvly-secret"})
    body = resp.json()
    # The key is stored (key_set True, effective True) but NEVER echoed anywhere in the response.
    assert body["key_set"] is True and body["effective"] is True
    assert "tvly-secret" not in resp.text
    assert store.values["tavily_api_key"] == "tvly-secret"


def test_toggle_off_makes_it_not_effective(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, _ = client
    test_client.put("/api/settings/tavily", json={"api_key": "tvly-secret"})
    body = test_client.put("/api/settings/tavily", json={"enabled": False}).json()
    # Toggling off leaves the key set but the tier not effective; toggling doesn't wipe the key.
    assert body["enabled"] is False and body["key_set"] is True and body["effective"] is False


def test_clear_key_reverts_to_keyless(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    test_client.put("/api/settings/tavily", json={"api_key": "tvly-secret"})
    body = test_client.put("/api/settings/tavily", json={"clear_key": True}).json()
    assert body["key_set"] is False and store.values["tavily_api_key"] == ""


def test_env_fallback_key_reads_as_set() -> None:
    # A key supplied only via the env fallback (JBRAIN_TAVILY_API_KEY) reads as present even with
    # no stored key — the same precedence the WebFetcher uses.
    app = create_app(_settings(tavily_api_key="env-key"))
    with TestClient(app) as test_client:
        app.state.auth_repo = FakeAuthRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        test_client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        store = FakeSettingsStore()
        app.state.settings_store = store
        _wire_fetcher(app, store)
        assert test_client.get("/api/settings/tavily").json()["key_set"] is True


def test_test_key_probe_reports_success(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, _ = client
    test_client.put("/api/settings/tavily", json={"api_key": "tvly-secret"})
    body = test_client.post("/api/settings/tavily/test", json={}).json()
    assert body["ok"] is True and "key works" in body["detail"]


def test_test_key_probe_reports_a_key_rejection_distinctly(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    # A 401 reads as a KEY REJECTION with the fix hint — not a vague "no page came back" (the
    # ambiguity that made a bad key look like a code bug).
    test_client, app, store = client
    _wire_fetcher(app, store, tavily_status=401)  # Tavily rejects the key
    test_client.put("/api/settings/tavily", json={"api_key": "bad-key"})
    body = test_client.post("/api/settings/tavily/test", json={}).json()
    assert body["ok"] is False
    assert "rejected the key (HTTP 401)" in body["detail"] and "tvly-" in body["detail"]


def test_test_key_probe_when_disabled_says_so(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, _ = client
    test_client.put("/api/settings/tavily", json={"api_key": "tvly-secret", "enabled": False})
    body = test_client.post("/api/settings/tavily/test", json={}).json()
    assert body["ok"] is False and "off" in body["detail"]  # names the disabled tier, not a miss
