"""The /api/settings/gmail surface — owner-only credential entry + a connection test,
with Gmail's HTTP faked (no network). Secrets are stored but never echoed back."""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.gmail import GmailClientProvider
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


def _gmail_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            # Covers both grants: access_token for the refresh flow (test endpoint),
            # refresh_token for the authorization-code exchange (Connect callback).
            return httpx.Response(
                200,
                json={"access_token": "tok", "expires_in": 3600, "refresh_token": "rt-from-google"},
            )
        return httpx.Response(
            200,
            json={"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "L1", "name": "Finance"}]},
        )

    return httpx.MockTransport(handle)


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FastAPI, FakeSettingsStore]]:
    app = create_app(_settings(public_base_url="https://box.example"))
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
        "client_id": "",
        "redirect_uri": "https://box.example/api/settings/gmail/callback",
    }


def test_put_sets_credentials_and_connects(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    body = test_client.put(
        "/api/settings/gmail",
        json={"client_id": "cid", "client_secret": "zzsecretval", "refresh_token": "zztokenval"},
    ).json()
    assert body["connected"] is True
    assert body == {
        "client_id_set": True,
        "client_secret_set": True,
        "refresh_token_set": True,
        "connected": True,
        "client_id": "cid",  # client_id is public, echoed back for verification
        "redirect_uri": "https://box.example/api/settings/gmail/callback",
    }
    # Stored, so the provider picks it up live...
    assert store.values["gmail_refresh_token"] == "zztokenval"
    # ...but the secret + refresh token are never echoed back (distinctive values that
    # can't collide with field names like client_secret_set).
    text = test_client.get("/api/settings/gmail").text
    assert "zzsecretval" not in text and "zztokenval" not in text


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


def _save_creds(test_client: TestClient) -> None:
    test_client.put(
        "/api/settings/gmail",
        json={"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec"},
    )


def test_connect_redirects_to_google_consent(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    _save_creds(test_client)
    resp = test_client.get("/api/settings/gmail/connect", follow_redirects=False)
    assert resp.status_code in (302, 307)
    loc = resp.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid.apps.googleusercontent.com" in loc
    assert "redirect_uri=https%3A%2F%2Fbox.example%2Fapi%2Fsettings%2Fgmail%2Fcallback" in loc
    assert "access_type=offline" in loc and "prompt=consent" in loc
    # A single-use CSRF state was stashed for the callback to check.
    assert store.values["gmail_oauth_state"]
    assert f"state={store.values['gmail_oauth_state']}" in loc


def test_connect_requires_saved_credentials(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, _ = client
    assert test_client.get("/api/settings/gmail/connect", follow_redirects=False).status_code == 400


def test_callback_exchanges_code_and_stores_token(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    _save_creds(test_client)
    test_client.get("/api/settings/gmail/connect", follow_redirects=False)
    state = store.values["gmail_oauth_state"]

    resp = test_client.get(
        f"/api/settings/gmail/callback?code=auth-code&state={state}", follow_redirects=False
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "https://box.example/settings?gmail=connected"
    assert store.values["gmail_refresh_token"] == "rt-from-google"  # minted via the code exchange
    assert store.values["gmail_oauth_state"] == ""  # single-use state cleared


def test_callback_rejects_a_bad_state(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    test_client, _, store = client
    _save_creds(test_client)
    test_client.get("/api/settings/gmail/connect", follow_redirects=False)

    resp = test_client.get(
        "/api/settings/gmail/callback?code=auth-code&state=forged", follow_redirects=False
    )
    assert resp.headers["location"] == "https://box.example/settings?gmail=error"
    assert "gmail_refresh_token" not in store.values  # nothing stored on a state mismatch


def test_connect_derives_redirect_uri_from_request_without_public_base() -> None:
    """No public_base_url set: the redirect_uri is derived from the host the browser
    hit, so a tunneled box works after a plain redeploy (no env editing)."""
    app = create_app(_settings())  # public_base_url left empty
    with TestClient(app) as test_client:
        app.state.auth_repo = FakeAuthRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        test_client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        store = FakeSettingsStore()
        app.state.settings_store = store
        app.state.gmail_provider = GmailClientProvider(
            store,
            _settings(),
            base_url="https://gmail.googleapis.com/gmail/v1",
            token_url="https://oauth2.googleapis.com/token",
            transport=_gmail_transport(),
        )
        test_client.put("/api/settings/gmail", json={"client_id": "cid", "client_secret": "sec"})
        resp = test_client.get("/api/settings/gmail/connect", follow_redirects=False)
        assert resp.status_code in (302, 307)
        # Derived from the request host (TestClient → testserver) since no public base.
        assert (
            "redirect_uri=http%3A%2F%2Ftestserver%2Fapi%2Fsettings%2Fgmail%2Fcallback"
            in resp.headers["location"]
        )


def test_redirect_uri_defaults_to_https_for_a_public_host() -> None:
    """No public_base_url: a request from a public hostname derives an https redirect_uri
    even when the tunnel drops x-forwarded-proto OR forwards it as plain http (the origin
    hop is http) — Google rejects non-https, so a tunnelled box connects after a plain
    update, no env edit. Loopback/test hosts still honour the raw scheme (covered above)."""
    app = create_app(_settings())  # public_base_url left empty
    with TestClient(app) as test_client:
        app.state.auth_repo = FakeAuthRepo()
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        test_client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        store = FakeSettingsStore()
        app.state.settings_store = store
        app.state.gmail_provider = GmailClientProvider(
            store,
            _settings(),
            base_url="https://gmail.googleapis.com/gmail/v1",
            token_url="https://oauth2.googleapis.com/token",
            transport=_gmail_transport(),
        )
        want = "https://hopkinsbrain.com/api/settings/gmail/callback"
        # No proto header at all (the tunnel dropped it)...
        no_proto = test_client.get("/api/settings/gmail", headers={"host": "hopkinsbrain.com"})
        assert no_proto.json()["redirect_uri"] == want
        # ...and a literal x-forwarded-proto: http (the origin hop) is overridden too.
        http_proto = test_client.get(
            "/api/settings/gmail",
            headers={"host": "hopkinsbrain.com", "x-forwarded-proto": "http"},
        )
        assert http_proto.json()["redirect_uri"] == want


def test_put_normalizes_a_url_ified_client_id(
    client: tuple[TestClient, FastAPI, FakeSettingsStore],
) -> None:
    """Mobile keyboards sometimes paste a client_id as http://…/. Store the bare id so
    Google doesn't reject it as invalid_client."""
    test_client, _, store = client
    test_client.put(
        "/api/settings/gmail",
        json={"client_id": "http://460754514015-abc.apps.googleusercontent.com/"},
    )
    assert store.values["gmail_client_id"] == "460754514015-abc.apps.googleusercontent.com"
