"""Start/stop endpoints: toggle a profile-gated service (comfyui), known-service
gate, and 202 semantics. Mirrors /restart, minus the self/all orchestration."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import AUTH, FakeGateway


def test_start_unknown_service_is_404(client: TestClient, gateway: FakeGateway) -> None:
    response = client.post("/start", json={"service": "nope"}, headers=AUTH)
    assert response.status_code == 404
    assert gateway.started == []


def test_stop_unknown_service_is_404(client: TestClient, gateway: FakeGateway) -> None:
    response = client.post("/stop", json={"service": "nope"}, headers=AUTH)
    assert response.status_code == 404
    assert gateway.stopped == []


def test_start_service(client: TestClient, gateway: FakeGateway) -> None:
    response = client.post("/start", json={"service": "api"}, headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"service": "api", "action": "start"}
    assert gateway.started == ["api"]


def test_stop_service(client: TestClient, gateway: FakeGateway) -> None:
    response = client.post("/stop", json={"service": "api"}, headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"service": "api", "action": "stop"}
    assert gateway.stopped == ["api"]


def test_start_requires_token(client: TestClient, gateway: FakeGateway) -> None:
    assert client.post("/start", json={"service": "api"}).status_code == 401
    assert gateway.started == []
