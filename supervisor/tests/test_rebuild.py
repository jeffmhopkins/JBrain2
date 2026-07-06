"""Per-service rebuild endpoint: validation, trigger, conflict, and status."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH, FakeGateway


def test_rebuild_requires_token(client: TestClient) -> None:
    assert client.post("/rebuild", json={"service": "api"}).status_code == 401
    assert client.get("/rebuild/status").status_code == 401


def test_rebuild_triggers_oneshot_for_the_service(
    client: TestClient, gateway: FakeGateway
) -> None:
    resp = client.post("/rebuild", json={"service": "api"}, headers=AUTH)
    assert resp.status_code == 202
    assert resp.json()["oneshot"].startswith("jbrain-rebuild-")
    assert ("rebuild", "api") in gateway.oneshots_started


def test_rebuild_unknown_service_is_404(
    client: TestClient, gateway: FakeGateway
) -> None:
    resp = client.post("/rebuild", json={"service": "nope"}, headers=AUTH)
    assert resp.status_code == 404
    assert (
        gateway.oneshots_started == []
    )  # never launched a one-shot for a bogus service


def test_rebuild_conflicts_with_a_running_oneshot(client: TestClient) -> None:
    assert (
        client.post("/rebuild", json={"service": "api"}, headers=AUTH).status_code
        == 202
    )
    # A second one-shot (another rebuild or an update) can't race the first.
    assert (
        client.post(
            "/rebuild", json={"service": "supervisor"}, headers=AUTH
        ).status_code
        == 409
    )
    assert client.post("/update", headers=AUTH).status_code == 409


def test_rebuild_status_lifecycle(client: TestClient, gateway: FakeGateway) -> None:
    none = client.get("/rebuild/status", headers=AUTH).json()
    assert none == {"state": "none", "exit_code": None, "log_tail": ""}

    client.post("/rebuild", json={"service": "api"}, headers=AUTH)
    running = client.get("/rebuild/status", headers=AUTH).json()
    assert running["state"] == "running"

    gateway.oneshot_running = None
    done = client.get("/rebuild/status", headers=AUTH).json()
    assert done["state"] == "exited"
    assert done["exit_code"] == 0
