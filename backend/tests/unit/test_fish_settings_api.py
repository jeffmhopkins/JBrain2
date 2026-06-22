"""The /api/settings/fish surface — owner-only status + free + service control for
the fishial service — with the gateway faked (no network, no GPU)."""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.fish_id.gateway import FishIdGatewayError, FishIdStatus
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


class _FakeGateway:
    """In-memory stand-in for FishIdGatewayClient (status + free only)."""

    def __init__(self, status: FishIdStatus, *, fail_free: bool = False) -> None:
        self._status = status
        self.fail_free = fail_free
        self.freed = False

    async def status(self) -> FishIdStatus:
        return self._status

    async def free(self) -> None:
        if self.fail_free:
            raise FishIdGatewayError("boom")
        self.freed = True


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FastAPI]]:
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
        yield test_client, app


def _enable(app: FastAPI, gateway: _FakeGateway, models: list[str]) -> None:
    """Flip fish hosting on for one test (SettingsDep reads app.state.settings)."""
    app.state.settings = _settings(
        fish_id_url="http://fish-id:8200", fish_id_models=models, supervisor_token="sup-secret"
    )
    app.state.fish_id_gateway = gateway


def test_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/fish").status_code == 401


def test_disabled_lists_catalog_but_reports_off(client: tuple[TestClient, FastAPI]) -> None:
    test_client, _ = client
    body = test_client.get("/api/settings/fish").json()
    assert body["enabled"] is False and body["reachable"] is False and body["loaded"] is False
    ids = {m["id"] for m in body["models"]}
    assert "fishial-v2" in ids
    assert all(m["enabled"] is False and m["disk_gb"] is None for m in body["models"])


def test_free_409_when_disabled(client: tuple[TestClient, FastAPI]) -> None:
    test_client, _ = client
    assert test_client.post("/api/settings/fish/free").status_code == 409


def test_enabled_lists_models_and_loaded_flag(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(FishIdStatus(reachable=True, loaded=True)), models=["fishial-v2"])
    body = test_client.get("/api/settings/fish").json()
    assert body["enabled"] is True and body["reachable"] is True and body["loaded"] is True
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["fishial-v2"]["enabled"] is True
    assert by_id["fishial-v2"]["arch"] == "DINOv2+ViT"
    assert by_id["fishial-v2"]["species_count"] == 866


def test_enabled_but_unreachable_is_idle(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(FishIdStatus(reachable=False)), models=["fishial-v2"])
    body = test_client.get("/api/settings/fish").json()
    assert body["enabled"] is True and body["reachable"] is False and body["loaded"] is False


def test_free_calls_gateway(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    gw = _FakeGateway(FishIdStatus(reachable=True, loaded=True))
    _enable(app, gw, models=["fishial-v2"])
    assert test_client.post("/api/settings/fish/free").status_code == 200
    assert gw.freed is True


def test_free_502_on_gateway_error(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(FishIdStatus(reachable=True), fail_free=True), models=["fishial-v2"])
    assert test_client.post("/api/settings/fish/free").status_code == 502


def _install_supervisor(app: FastAPI, status_code: int, seen: list[tuple[str, Any, str]]) -> None:
    """Replace the supervisor proxy client with one that records the call and returns
    `status_code` (so start/stop can be driven without a real supervisor)."""

    def handle(req: httpx.Request) -> httpx.Response:
        import json

        seen.append((req.url.path, json.loads(req.content), req.headers.get("authorization", "")))
        return httpx.Response(status_code, json={})

    app.state.supervisor_client = httpx.AsyncClient(
        base_url="http://supervisor:9000", transport=httpx.MockTransport(handle)
    )


@pytest.mark.parametrize("action", ["start", "stop"])
def test_service_toggle_proxies_to_supervisor(
    client: tuple[TestClient, FastAPI], action: str
) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(FishIdStatus(reachable=True)), models=["fishial-v2"])
    seen: list[tuple[str, Any, str]] = []
    _install_supervisor(app, 202, seen)
    resp = test_client.post(f"/api/settings/fish/service/{action}")
    assert resp.status_code == 202
    assert resp.json() == {"service": "fish-id", "action": action}
    # The proxy hits the matching supervisor command for the fish-id service AND carries
    # the supervisor bearer token (its only auth) — never a user-controlled service name.
    assert seen == [(f"/{action}", {"service": "fish-id"}, "Bearer sup-secret")]


def test_service_start_404_when_not_provisioned(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(FishIdStatus(reachable=False)), models=["fishial-v2"])
    _install_supervisor(app, 404, [])
    assert test_client.post("/api/settings/fish/service/start").status_code == 404


def test_service_start_409_when_disabled(client: tuple[TestClient, FastAPI]) -> None:
    test_client, _ = client
    assert test_client.post("/api/settings/fish/service/start").status_code == 409
