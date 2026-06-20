"""Pairing config builder + the mint/redeem endpoints (fakes, no Postgres)."""

import base64
import json
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api.deps import current_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.locations.pairing import (
    RedeemedDevice,
    build_owntracks_config,
    build_pairing_payload,
    generate_pairing_code,
)
from jbrain.locations.ratelimit import TokenBucket
from jbrain.main import create_app
from tests.unit.fakes import FakePairingRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"


def _decode_payload(payload: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


# --- config builder + code generation ---------------------------------------


def test_build_owntracks_config_binds_username_to_principal() -> None:
    dev = RedeemedDevice(
        subject_id="s", principal_id="pid-123", label="Mom", monitoring=2, key="jb1-KEY"
    )
    cfg = build_owntracks_config(dev, broker_host="mqtt.example.com", broker_port=8883)
    assert cfg["mode"] == 0 and cfg["tls"] is True
    # The M0 ACL binds the MQTT username to the principal id.
    assert cfg["username"] == "pid-123" and cfg["clientId"] == "pid-123"
    assert cfg["pubTopicBase"] == "owntracks/pid-123/phone"
    assert cfg["password"] == "jb1-KEY"
    assert cfg["monitoring"] == 2
    assert cfg["remoteConfiguration"] is True  # off upstream; we need server mode-switch
    assert cfg["host"] == "mqtt.example.com" and cfg["port"] == 8883


def test_generate_pairing_code_is_unique_and_high_entropy() -> None:
    codes = {generate_pairing_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(len(c) >= 20 for c in codes)


def test_build_pairing_payload_embeds_the_server_and_code() -> None:
    payload = build_pairing_payload("https://hopkinsbrain.com/", "CODE-123")
    decoded = _decode_payload(payload)
    assert decoded == {"v": 1, "u": "https://hopkinsbrain.com", "c": "CODE-123"}
    # Opaque + QR-safe: base64url, no padding.
    assert "=" not in payload and "+" not in payload and "/" not in payload


# --- endpoints ---------------------------------------------------------------


def _owner() -> PrincipalInfo:
    return PrincipalInfo(id="owner-1", kind="owner", label="o")


def test_mint_is_owner_only() -> None:
    app = create_app(Settings(secure_cookies=False, database_url=_DB))
    pairing = FakePairingRepo()
    with TestClient(app) as c:
        app.state.pairing_repo = pairing
        # Anonymous → 401.
        assert c.post("/api/pairing/codes", json={"label": "Mom"}).status_code == 401
        # A capability token → 403.
        cast(FastAPI, c.app).dependency_overrides[current_principal] = lambda: PrincipalInfo(
            id="cap", kind="capability_token", label="x"
        )
        try:
            assert c.post("/api/pairing/codes", json={"label": "Mom"}).status_code == 403
        finally:
            cast(FastAPI, c.app).dependency_overrides.clear()
        # The owner → 201 with a code.
        cast(FastAPI, c.app).dependency_overrides[current_principal] = _owner
        try:
            r = c.post("/api/pairing/codes", json={"label": "Mom", "monitoring": 2})
            assert r.status_code == 201
            assert r.json()["code"] == "fake-code"
            assert pairing.minted == [("Mom", 2)]
        finally:
            cast(FastAPI, c.app).dependency_overrides.clear()


def test_mint_returns_an_embeddable_payload_with_the_server_url() -> None:
    app = create_app(
        Settings(secure_cookies=False, database_url=_DB, dashboard_url="https://hopkinsbrain.com")
    )
    with TestClient(app) as c:
        app.state.pairing_repo = FakePairingRepo()
        cast(FastAPI, c.app).dependency_overrides[current_principal] = _owner
        try:
            body = c.post("/api/pairing/codes", json={"label": "Phone"}).json()
        finally:
            cast(FastAPI, c.app).dependency_overrides.clear()
    # The single payload string carries the server + the code the app redeems.
    decoded = _decode_payload(body["payload"])
    assert decoded["u"] == "https://hopkinsbrain.com"
    assert decoded["c"] == body["code"]


def test_redeem_returns_config_for_a_valid_code_and_400_otherwise() -> None:
    app = create_app(
        Settings(
            secure_cookies=False,
            database_url=_DB,
            mqtt_public_host="mqtt.example.com",
            dashboard_url="https://dash.example.com",
        )
    )
    dev = RedeemedDevice(
        subject_id="s", principal_id="pid-9", label="Mom", monitoring=1, key="jb1-K"
    )
    with TestClient(app) as c:
        app.state.pairing_repo = FakePairingRepo(redeemable={"good": dev})
        r = c.post("/api/pairing/redeem", json={"code": "good"})
        assert r.status_code == 200
        body = r.json()
        assert body["config"]["username"] == "pid-9"
        assert body["config"]["password"] == "jb1-K"
        assert body["dashboard_url"] == "https://dash.example.com"
        # An unknown / expired / used code is a flat 400 (no oracle).
        assert c.post("/api/pairing/redeem", json={"code": "bad"}).status_code == 400


def test_redeem_is_rate_limited() -> None:
    app = create_app(Settings(secure_cookies=False, database_url=_DB, mqtt_public_host="h"))
    with TestClient(app) as c:
        app.state.pairing_repo = FakePairingRepo()
        app.state.pairing_rate_limiter = TokenBucket(capacity=1, refill_per_sec=0.0)
        # First attempt spends the only token (invalid code → 400); the next is 429.
        assert c.post("/api/pairing/redeem", json={"code": "x"}).status_code == 400
        assert c.post("/api/pairing/redeem", json={"code": "x"}).status_code == 429
