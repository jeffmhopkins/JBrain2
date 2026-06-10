"""Status endpoint maps gateway snapshots to the wire shape."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import AUTH


def test_status_maps_containers(client: TestClient) -> None:
    response = client.get("/status", headers=AUTH)

    assert response.status_code == 200
    containers = response.json()["containers"]
    assert containers == [
        {
            "service": "api",
            "state": "running",
            "health": "healthy",
            "started_at": "2026-06-10T00:00:00Z",
            "image": "ghcr.io/jeff/jbrain-api:edge",
        },
        {
            "service": "postgres",
            "state": "running",
            "health": "healthy",
            "started_at": "2026-06-10T00:00:00Z",
            "image": "postgres:16",
        },
        {
            "service": "supervisor",
            "state": "running",
            "health": None,
            "started_at": "2026-06-10T00:00:00Z",
            "image": "ghcr.io/jeff/jbrain-supervisor:edge",
        },
    ]
