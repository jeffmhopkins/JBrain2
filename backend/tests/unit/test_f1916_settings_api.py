"""The /api/settings/1f916 surface — owner-only register/rotate/test for jerv's
1f916.ai citizenship (docs/plans/F1916_CITIZENSHIP_PLAN.md W1). The platform's HTTP
is faked; the one-time secret is consumed server-side and appears in NO response.
This is a security path (CLAUDE.md rule 5): the custody assertions here are the
contract — the secret reaches the settings store and nothing else."""

import asyncio
import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.web.f1916 import F1916Client, F1916Creds
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore

SECRET = "1f916_sk_test_00aa11bb"


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


class FakePlatform:
    """A scriptable 1f916.ai: records requests, answers register/rotate/me."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.register_response: dict | int = {"ok": True, "secret": SECRET, "now_utc": "2026-08-24"}
        self.rotate_secret = "1f916_sk_rotated_99"
        self.rotate_status = 200
        self.me_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/register":
            if isinstance(self.register_response, int):
                return httpx.Response(self.register_response, json={"error": "handle is taken"})
            return httpx.Response(200, json=self.register_response)
        if request.url.path == "/api/rotate":
            if self.rotate_status != 200:
                return httpx.Response(self.rotate_status, json={"error": "slow down"})
            body = {"secret": self.rotate_secret} if self.rotate_secret else {"ok": True}
            return httpx.Response(200, json=body)
        if request.url.path == "/api/me":
            if self.me_status != 200:
                return httpx.Response(self.me_status, json={"error": "No credentials: …"})
            return httpx.Response(200, json={"replies": []})
        return httpx.Response(404, json={"error": "no such route"})


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeSettingsStore, FakePlatform]]:
    app: FastAPI = create_app(_settings())
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
        platform = FakePlatform()

        async def creds() -> F1916Creds:
            return F1916Creds(
                enabled=await store.f1916_enabled(None),
                handle=await store.f1916_handle(None),
                secret=await store.f1916_secret_key(None),
            )

        app.state.f1916_client = F1916Client(
            "https://1f916.ai", creds=creds, transport=httpx.MockTransport(platform)
        )
        yield test_client, store, platform


def test_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/1f916").status_code == 401


def test_starts_enabled_and_unregistered(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, _, _ = client
    assert test_client.get("/api/settings/1f916").json() == {
        "enabled": True,
        "registered": False,
        "handle": "",
        "signing_key_set": False,
    }


def test_register_stores_the_secret_and_never_echoes_it(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, platform = client
    resp = test_client.post(
        "/api/settings/1f916/register", json={"handle": "jerv", "model": "gpt-oss-120b"}
    )
    body = resp.json()
    assert body["ok"] is True and body["status"]["registered"] is True
    assert body["status"]["handle"] == "jerv" and body["status"]["signing_key_set"] is True
    # Custody: the secret reached the store and NOTHING else — not the response, and
    # the live check ran with it as the bearer.
    assert SECRET not in resp.text
    assert store.values["f1916_secret_key"] == SECRET
    me = [r for r in platform.requests if r.url.path == "/api/me"]
    assert me and me[0].headers["Authorization"] == f"Bearer {SECRET}"


def test_register_binds_a_valid_ed25519_key(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, platform = client
    test_client.post(
        "/api/settings/1f916/register", json={"handle": "jerv", "model": "gpt-oss-120b"}
    )
    sent = json.loads(next(r for r in platform.requests if r.url.path == "/api/register").content)

    # The signature verifies over the platform's documented key-bind statement, and the
    # stored private half round-trips to the public key that left the box.
    def unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    public = Ed25519PublicKey.from_public_bytes(unb64(sent["public_key"]))
    statement = f"1f916.key-bind.v1:jerv:{sent['public_key']}".encode()
    public.verify(unb64(sent["signature"]), statement)  # raises on mismatch
    assert isinstance(store.values["f1916_signing_key"], str)
    assert store.values["f1916_public_key"] == sent["public_key"]
    assert len(unb64(store.values["f1916_signing_key"])) == 32


def test_register_twice_is_refused_the_platform_is_never_hit(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, _, platform = client
    test_client.post(
        "/api/settings/1f916/register", json={"handle": "jerv", "model": "gpt-oss-120b"}
    )
    before = len(platform.requests)
    body = test_client.post(
        "/api/settings/1f916/register", json={"handle": "jerv2", "model": "x"}
    ).json()
    assert body["ok"] is False and "already registered" in body["detail"]
    assert len(platform.requests) == before  # a second citizen was never minted


def test_register_with_no_secret_in_the_response_stores_nothing(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, platform = client
    platform.register_response = {"ok": True, "note": "welcome"}  # no 1f916_sk_ anywhere
    body = test_client.post(
        "/api/settings/1f916/register", json={"handle": "jerv", "model": "x"}
    ).json()
    assert body["ok"] is False and "no citizen secret could be found" in body["detail"]
    assert "f1916_secret_key" not in store.values


def test_register_refusal_from_the_platform_is_surfaced(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, platform = client
    platform.register_response = 409
    body = test_client.post(
        "/api/settings/1f916/register", json={"handle": "jerv", "model": "x"}
    ).json()
    assert body["ok"] is False and "handle is taken" in body["detail"]
    assert "f1916_secret_key" not in store.values


def test_a_bad_handle_fails_before_the_wire(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, _, platform = client
    body = test_client.post(
        "/api/settings/1f916/register", json={"handle": "-bad handle-", "model": "x"}
    ).json()
    assert body["ok"] is False and "Handle must be" in body["detail"]
    assert platform.requests == []


def test_rotate_replaces_the_secret_without_echoing_either(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, platform = client
    test_client.post("/api/settings/1f916/register", json={"handle": "jerv", "model": "x"})
    resp = test_client.post("/api/settings/1f916/rotate")
    body = resp.json()
    assert body["ok"] is True
    assert store.values["f1916_secret_key"] == platform.rotate_secret
    assert SECRET not in resp.text and platform.rotate_secret not in resp.text
    # The rotate call itself authenticated with the OLD secret.
    rot = next(r for r in platform.requests if r.url.path == "/api/rotate")
    assert rot.headers["Authorization"] == f"Bearer {SECRET}"


def test_rotate_without_a_citizen_refuses(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, _, _ = client
    body = test_client.post("/api/settings/1f916/rotate").json()
    assert body["ok"] is False and "no secret to rotate" in body["detail"]


def test_the_test_probe_reports_a_rejected_secret_distinctly(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, _, platform = client
    test_client.post("/api/settings/1f916/register", json={"handle": "jerv", "model": "x"})
    platform.me_status = 401
    body = test_client.post("/api/settings/1f916/test").json()
    assert body["ok"] is False and "rejected the stored secret (401)" in body["detail"]
    platform.me_status = 200
    assert test_client.post("/api/settings/1f916/test").json()["ok"] is True


def test_toggle_persists_and_survives_reads(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, _ = client
    body = test_client.put("/api/settings/1f916", json={"enabled": False}).json()
    assert body["enabled"] is False and store.values["f1916_enabled"] is False
    assert test_client.get("/api/settings/1f916").json()["enabled"] is False


def test_register_detects_a_store_that_failed_to_persist(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    # A store write that silently drops the secret is the "registered but unstored"
    # lethal case — the handler must read back and report it, never claim success.
    test_client, store, _ = client

    async def dropping_set(ctx: object, **kw: object) -> None:
        store.values["f1916_handle"] = kw["handle"]  # everything but the secret persists

    store.set_f1916_citizen = dropping_set  # type: ignore[method-assign]
    body = test_client.post(
        "/api/settings/1f916/register", json={"handle": "jerv", "model": "x"}
    ).json()
    assert body["ok"] is False and "failed to read back" in body["detail"]


def test_rotate_refused_by_the_platform_keeps_the_old_secret(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, platform = client
    test_client.post("/api/settings/1f916/register", json={"handle": "jerv", "model": "x"})

    platform.rotate_status = 429
    body = test_client.post("/api/settings/1f916/rotate").json()
    assert body["ok"] is False and "refused the rotation" in body["detail"]
    assert store.values["f1916_secret_key"] == SECRET  # the old secret still stands


def test_rotate_with_no_secret_in_the_response_says_the_citizen_may_be_lost(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, store, platform = client
    test_client.post("/api/settings/1f916/register", json={"handle": "jerv", "model": "x"})
    platform.rotate_secret = ""  # the response carries nothing shaped like a secret
    body = test_client.post("/api/settings/1f916/rotate").json()
    assert body["ok"] is False and "no new secret could be found" in body["detail"]
    assert store.values["f1916_secret_key"] == SECRET  # the stored copy is untouched


def test_the_probe_reports_a_non_401_failure_verbatim(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, _, platform = client
    test_client.post("/api/settings/1f916/register", json={"handle": "jerv", "model": "x"})
    platform.me_status = 503
    body = test_client.post("/api/settings/1f916/test").json()
    assert body["ok"] is False and "No credentials" in body["detail"]


def test_the_probe_without_a_citizen_refuses(
    client: tuple[TestClient, FakeSettingsStore, FakePlatform],
) -> None:
    test_client, _, _ = client
    body = test_client.post("/api/settings/1f916/test").json()
    assert body["ok"] is False and "No citizen is registered yet" in body["detail"]
