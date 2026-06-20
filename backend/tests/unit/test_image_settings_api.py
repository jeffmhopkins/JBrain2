"""The /api/settings/image surface — owner-only status + free for the ComfyUI
image service — with the gateway faked (no network, no GPU)."""

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.image_gen.gateway import ComfyUiGatewayError, GatewayStatus
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    return Settings(**kw)


class _FakeGateway:
    """In-memory stand-in for ComfyUiGatewayClient."""

    def __init__(
        self, status: GatewayStatus, *, fail_free: bool = False, fail_interrupt: bool = False
    ) -> None:
        self._status = status
        self.fail_free = fail_free
        self.fail_interrupt = fail_interrupt
        self.freed = False
        self.interrupted = False

    async def status(self) -> GatewayStatus:
        return self._status

    async def free(self, *, unload_models: bool = True, free_memory: bool = True) -> None:
        if self.fail_free:
            raise ComfyUiGatewayError("boom")
        self.freed = True

    async def interrupt(self) -> None:
        if self.fail_interrupt:
            raise ComfyUiGatewayError("boom")
        self.interrupted = True


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
    """Flip image hosting on for one test (SettingsDep reads app.state.settings)."""
    app.state.settings = _settings(comfyui_url="http://comfyui:8188", comfyui_models=models)
    app.state.comfyui_gateway = gateway


def test_requires_auth() -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/settings/image").status_code == 401


def test_disabled_lists_catalog_but_reports_off(client: tuple[TestClient, FastAPI]) -> None:
    test_client, _ = client
    body = test_client.get("/api/settings/image").json()
    assert body["enabled"] is False and body["reachable"] is False
    assert body["memory"] is None
    # The catalog is still shown (so the operator sees what they could provision),
    # but nothing is enabled and disk sizes are absent off-box.
    ids = {m["id"] for m in body["models"]}
    assert "qwen-image" in ids
    assert all(m["enabled"] is False and m["disk_gb"] is None for m in body["models"])


def test_free_409_when_disabled(client: tuple[TestClient, FastAPI]) -> None:
    test_client, _ = client
    assert test_client.post("/api/settings/image/free").status_code == 409


def test_enabled_lists_models_and_real_vram(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(
        app,
        _FakeGateway(GatewayStatus(reachable=True, vram_total_gb=128.0, vram_free_gb=96.0)),
        models=["qwen-image"],
    )
    body = test_client.get("/api/settings/image").json()
    assert body["enabled"] is True and body["reachable"] is True
    assert body["memory"] == {"total_gb": 128.0, "free_gb": 96.0}
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["qwen-image"]["enabled"] is True
    assert by_id["qwen-image-edit"]["enabled"] is False  # not in the provisioned set


def test_enabled_but_unreachable_has_no_memory(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(GatewayStatus(reachable=False)), models=["qwen-image"])
    body = test_client.get("/api/settings/image").json()
    assert body["enabled"] is True and body["reachable"] is False and body["memory"] is None


def test_free_calls_gateway(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    gw = _FakeGateway(GatewayStatus(reachable=True, vram_total_gb=128.0, vram_free_gb=128.0))
    _enable(app, gw, models=["qwen-image"])
    assert test_client.post("/api/settings/image/free").status_code == 200
    assert gw.freed is True


def test_free_502_on_gateway_error(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(GatewayStatus(reachable=True), fail_free=True), models=["qwen-image"])
    assert test_client.post("/api/settings/image/free").status_code == 502


def _install_supervisor(app: FastAPI, status_code: int, seen: list[tuple[str, Any]]) -> None:
    """Replace the supervisor proxy client with one that records the call and
    returns `status_code` (so start/stop can be driven without a real supervisor)."""

    def handle(req: httpx.Request) -> httpx.Response:
        import json

        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(status_code, json={})

    app.state.supervisor_client = httpx.AsyncClient(
        base_url="http://supervisor:9000", transport=httpx.MockTransport(handle)
    )


@pytest.mark.parametrize("action", ["start", "stop"])
def test_service_toggle_proxies_to_supervisor(
    client: tuple[TestClient, FastAPI], action: str
) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(GatewayStatus(reachable=True)), models=["qwen-image"])
    seen: list[tuple[str, Any]] = []
    _install_supervisor(app, 202, seen)
    resp = test_client.post(f"/api/settings/image/service/{action}")
    assert resp.status_code == 202
    assert resp.json() == {"service": "comfyui", "action": action}
    # Proxied to the matching supervisor command for the comfyui service.
    assert seen == [(f"/{action}", {"service": "comfyui"})]


def test_service_start_404_when_not_provisioned(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(app, _FakeGateway(GatewayStatus(reachable=False)), models=["qwen-image"])
    _install_supervisor(app, 404, [])
    assert test_client.post("/api/settings/image/service/start").status_code == 404


def test_service_start_409_when_disabled(client: tuple[TestClient, FastAPI]) -> None:
    test_client, _ = client
    assert test_client.post("/api/settings/image/service/start").status_code == 409


def test_interrupt_calls_gateway(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    gw = _FakeGateway(GatewayStatus(reachable=True))
    _enable(app, gw, models=["qwen-image"])
    resp = test_client.post("/api/settings/image/interrupt")
    assert resp.status_code == 202 and resp.json() == {"status": "interrupted"}
    assert gw.interrupted is True


def test_interrupt_409_when_disabled(client: tuple[TestClient, FastAPI]) -> None:
    test_client, _ = client
    assert test_client.post("/api/settings/image/interrupt").status_code == 409


def test_interrupt_502_on_gateway_error(client: tuple[TestClient, FastAPI]) -> None:
    test_client, app = client
    _enable(
        app, _FakeGateway(GatewayStatus(reachable=True), fail_interrupt=True), models=["qwen-image"]
    )
    assert test_client.post("/api/settings/image/interrupt").status_code == 502
