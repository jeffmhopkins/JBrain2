"""The owner /api/settings/debug-tokens lifecycle (mint/list/revoke) + the
self-contained payload encoding, with the auth repo faked."""

import asyncio
import base64
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.api.debug_tokens import build_debug_payload
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"


def _decode(payload: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_build_debug_payload_embeds_host_and_key() -> None:
    payload = build_debug_payload("https://brain.example.com/", "SECRET-KEY")
    assert _decode(payload) == {"v": 1, "u": "https://brain.example.com", "k": "SECRET-KEY"}
    # Opaque, QR-/copy-safe: base64url, no padding.
    assert "=" not in payload and "+" not in payload and "/" not in payload


def _settings(**kw: object) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", _DB)
    return Settings(**kw)  # type: ignore[arg-type]


@pytest.fixture
def owner_client() -> Iterator[tuple[TestClient, FakeAuthRepo]]:
    app = create_app(
        _settings(debug_access_enabled=True, dashboard_url="https://brain.example.com")
    )
    repo = FakeAuthRepo()
    with TestClient(app) as client:
        app.state.auth_repo = repo
        key = asyncio.run(auth_service.rotate_owner_key(repo))
        assert (
            client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        ).status_code == 204
        yield client, repo


def test_mint_list_revoke_roundtrip(owner_client: tuple[TestClient, FakeAuthRepo]) -> None:
    client, _ = owner_client
    minted = client.post("/api/settings/debug-tokens", json={"label": "claude", "ttl_hours": 12})
    assert minted.status_code == 201
    body = minted.json()
    assert _decode(body["payload"])["u"] == "https://brain.example.com"
    token_id = body["id"]

    listed = client.get("/api/settings/debug-tokens").json()
    assert [t["id"] for t in listed] == [token_id]
    assert listed[0]["label"] == "claude" and listed[0]["revoked_at"] is None

    assert client.delete(f"/api/settings/debug-tokens/{token_id}").status_code == 204
    # Re-revoking a now-revoked token 404s.
    assert client.delete(f"/api/settings/debug-tokens/{token_id}").status_code == 404


def test_public_base_url_points_handed_off_tokens_at_the_public_host() -> None:
    # public_base_url wins over the request origin, so a token minted from the LAN
    # PWA still embeds the public host for an external assistant to connect to.
    app = create_app(
        _settings(debug_access_enabled=True, public_base_url="https://pub.example.com/")
    )
    repo = FakeAuthRepo()
    with TestClient(app) as client:
        app.state.auth_repo = repo
        key = asyncio.run(auth_service.rotate_owner_key(repo))
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        minted = client.post(
            "/api/settings/debug-tokens",
            json={"label": "x"},
            headers={"origin": "https://jbrain.local"},
        )
        assert _decode(minted.json()["payload"])["u"] == "https://pub.example.com"


def test_mint_refused_when_feature_disabled() -> None:
    app = create_app(_settings(debug_access_enabled=False))
    repo = FakeAuthRepo()
    with TestClient(app) as client:
        app.state.auth_repo = repo
        key = asyncio.run(auth_service.rotate_owner_key(repo))
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        # The management routes exist, but minting is refused while the flag is off.
        assert client.post("/api/settings/debug-tokens", json={"label": "x"}).status_code == 409


def test_mint_requires_owner() -> None:
    app = create_app(_settings(debug_access_enabled=True))
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.post("/api/settings/debug-tokens", json={"label": "x"}).status_code == 401


def test_suspend_resume_roundtrip(owner_client: tuple[TestClient, FakeAuthRepo]) -> None:
    client, _ = owner_client
    token_id = client.post(
        "/api/settings/debug-tokens", json={"label": "claude", "ttl_hours": 12}
    ).json()["id"]

    # Suspend → the list reflects it; suspending again 404s (already suspended).
    assert client.post(f"/api/settings/debug-tokens/{token_id}/suspend").status_code == 204
    assert client.get("/api/settings/debug-tokens").json()[0]["suspended_at"] is not None
    assert client.post(f"/api/settings/debug-tokens/{token_id}/suspend").status_code == 404

    # Resume clears it; resuming an active token 404s.
    assert client.post(f"/api/settings/debug-tokens/{token_id}/resume").status_code == 204
    assert client.get("/api/settings/debug-tokens").json()[0]["suspended_at"] is None
    assert client.post(f"/api/settings/debug-tokens/{token_id}/resume").status_code == 404


def test_suspend_resume_require_owner() -> None:
    app = create_app(_settings(debug_access_enabled=True))
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.post("/api/settings/debug-tokens/x/suspend").status_code == 401
        assert anon.post("/api/settings/debug-tokens/x/resume").status_code == 401
