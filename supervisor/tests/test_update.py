"""Updater endpoints: trigger and conflict behavior."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH


def test_update_requires_token(client: TestClient) -> None:
    assert client.post("/update").status_code == 401
    assert client.get("/update/status").status_code == 401


def test_update_triggers_detached_updater(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/update", headers=AUTH)
    assert resp.status_code == 202
    assert resp.json()["updater"].startswith("jbrain-updater-")
    assert gateway.updates_started


def test_second_update_conflicts_while_running(client: TestClient) -> None:
    assert client.post("/update", headers=AUTH).status_code == 202
    assert client.post("/update", headers=AUTH).status_code == 409


def test_update_status_lifecycle(client: TestClient, gateway) -> None:  # type: ignore[no-untyped-def]
    none = client.get("/update/status", headers=AUTH).json()
    assert none == {"state": "none", "exit_code": None, "log_tail": ""}

    client.post("/update", headers=AUTH)
    running = client.get("/update/status", headers=AUTH).json()
    assert running["state"] == "running"
    assert running["exit_code"] is None

    gateway.updater_running = False
    done = client.get("/update/status", headers=AUTH).json()
    assert done["state"] == "exited"
    assert done["exit_code"] == 0
