"""The /api/settings/gmail surface — owner-only credential entry + a connection test,
with Gmail's HTTP faked (no network). Secrets are stored but never echoed back."""

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.gmail import GmailClientProvider
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


def _settings(**kw: object) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


def _gmail_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(
            200,
            json={"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "L1", "name": "Finance"}]},
        )

    return httpx.MockTransport(handle)


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
        app.state.gmail_provider = GmailClientProvider(
            store,
            _settings(),
            base_url="https://gmail.googleapis.com/gmail/v1",
            token_url="https://oauth2.googleapis.com/token",
            transport=_gmail_transport(),
        )
        yield test_client, app, store


def test_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/gmail").status_code == 401


def test_starts_disconnected(client: tuple[TestClient, FastAPI, FakeSettingsStore]) -> None:
    test_client, _, _ = client
    assert test_client.get("/api/settings/gmail").json() == {
        "client_id_set": False,
        "client_secret_set": False,
        "refresh_token_set": False,
        "connected": False,
    }


def test_put_sets_credentials_and_connects(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    body = test_client.put(
        "/api/settings/gmail",
        json={"client_id": "cid", "client_secret": "sec", "refresh_token": "rt"},
    ).json()
    assert body["connected"] is True
    assert body == {
        "client_id_set": True,
        "client_secret_set": True,
        "refresh_token_set": True,
        "connected": True,
    }
    # Stored, so the provider picks it up live...
    assert store.values["gmail_refresh_token"] == "rt"
    # ...but the secret is never echoed back in any response.
    assert "rt" not in test_client.get("/api/settings/gmail").text


def test_put_is_partial_and_does_not_wipe(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    test_client.put(
        "/api/settings/gmail",
        json={"client_id": "cid", "client_secret": "sec", "refresh_token": "rt"},
    )
    test_client.put("/api/settings/gmail", json={"refresh_token": "rt2"})  # only the token
    assert store.values["gmail_client_id"] == "cid"  # untouched
    assert store.values["gmail_refresh_token"] == "rt2"


def test_test_endpoint_reports_connection(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, _ = client
    assert test_client.post("/api/settings/gmail/test").json()["ok"] is False  # not connected yet
    test_client.put(
        "/api/settings/gmail",
        json={"client_id": "cid", "client_secret": "sec", "refresh_token": "rt"},
    )
    out = test_client.post("/api/settings/gmail/test").json()
    assert out["ok"] is True
    assert "labels" in out["detail"]
