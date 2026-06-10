"""Restart endpoint: known-service gate, self-last ordering, 202 semantics."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import AUTH, FakeGateway


def test_restart_unknown_service_is_404(
    client: TestClient, gateway: FakeGateway
) -> None:
    response = client.post("/restart", json={"service": "nope"}, headers=AUTH)

    assert response.status_code == 404
    assert gateway.restarted == []


def test_restart_single_service(client: TestClient, gateway: FakeGateway) -> None:
    response = client.post("/restart", json={"service": "api"}, headers=AUTH)

    assert response.status_code == 202
    assert response.json() == {"restarting": ["api"]}
    assert gateway.restarted == ["api"]


def test_restart_all_puts_self_last(client: TestClient, gateway: FakeGateway) -> None:
    # TestClient runs background tasks before returning, so the recorded
    # order includes the deferred self-restart.
    response = client.post("/restart", json={"service": "all"}, headers=AUTH)

    assert response.status_code == 202
    assert response.json() == {"restarting": ["api", "postgres", "supervisor"]}
    assert gateway.restarted == ["api", "postgres", "supervisor"]
    assert gateway.restarted[-1] == "supervisor"


def test_restart_self_is_deferred_but_runs(
    client: TestClient, gateway: FakeGateway
) -> None:
    response = client.post("/restart", json={"service": "supervisor"}, headers=AUTH)

    assert response.status_code == 202
    assert response.json() == {"restarting": ["supervisor"]}
    assert gateway.restarted == ["supervisor"]
