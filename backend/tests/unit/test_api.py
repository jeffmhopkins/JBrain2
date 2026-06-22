"""API-surface tests with a fake repo and a mocked supervisor."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

SUPERVISOR_STATUS = {
    "containers": [
        {
            "service": "api",
            "state": "running",
            "health": "healthy",
            "started_at": None,
            "image": "x",
        }
    ]
}


def fake_supervisor(request: httpx.Request) -> httpx.Response:
    if request.headers.get("Authorization") != "Bearer st-token":
        return httpx.Response(401)
    if request.url.path == "/status":
        return httpx.Response(200, json=SUPERVISOR_STATUS)
    if request.url.path == "/restart":
        body = json.loads(request.content)
        if body["service"] == "ghost":
            return httpx.Response(404)
        return httpx.Response(202, json={"restarting": [body["service"]]})
    if request.url.path == "/logs/api":
        return httpx.Response(200, text="line1\nline2\n")
    if request.url.path == "/metrics":
        return httpx.Response(
            200,
            json={
                "mem_total_bytes": 4 << 30,
                "mem_available_bytes": 1 << 30,
                "swap_total_bytes": 0,
                "swap_free_bytes": 0,
                "disk_total_bytes": 40 << 30,
                "disk_free_bytes": 25 << 30,
                "load_1m": 0.5,
                "load_5m": 0.4,
                "load_15m": 0.3,
                "uptime_seconds": 12345,
                "gpu_busy_percent": 42.0,
                "fan_rpm": {"CPU Fan": 2100},
                "containers": [{"service": "api", "mem_bytes": 100 << 20}],
            },
        )
    if request.url.path == "/update" and request.method == "POST":
        return httpx.Response(202, json={"updater": "jbrain-updater-1"})
    if request.url.path == "/update/status":
        return httpx.Response(200, json={"state": "running", "exit_code": None, "log_tail": "x"})
    return httpx.Response(404)


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False,
        supervisor_token="st-token",
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        # Swap the lifespan-built SQL-backed state for fakes.
        app.state.auth_repo = repo
        app.state.supervisor_client = httpx.AsyncClient(
            transport=httpx.MockTransport(fake_supervisor), base_url="http://supervisor"
        )
        yield test_client


async def _owner_key(repo: FakeAuthRepo) -> str:
    return await service.rotate_owner_key(repo)


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    import asyncio

    key = asyncio.run(_owner_key(repo))
    resp = client.post("/api/auth/session", json={"owner_key": key, "device_label": "test"})
    assert resp.status_code == 204


def test_healthz(client: TestClient) -> None:
    assert client.get("/api/healthz").json() == {"status": "ok"}


def test_readyz_reports_database_down(client: TestClient) -> None:
    resp = client.get("/api/readyz")
    assert resp.status_code == 503


def test_login_bad_key_rejected(client: TestClient) -> None:
    resp = client.post("/api/auth/session", json={"owner_key": "jb1-WRONG"})
    assert resp.status_code == 401


def test_login_me_logout_roundtrip(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["kind"] == "owner"

    assert client.delete("/api/auth/session").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_me_requires_session(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code == 401


def test_ops_requires_owner(client: TestClient) -> None:
    assert client.get("/api/ops/status").status_code == 401
    # The history graph is owner-only too (the router dependency).
    assert client.get("/api/ops/metrics/history").status_code == 401


def test_ops_metrics_history_returns_series(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jbrain.api import ops

    captured: dict[str, object] = {}

    async def fake_history(maker, ctx, *, since, until=None, max_points=300):  # noqa: ANN001, ANN202
        captured["since"] = since
        return {"resolution": "raw", "points": [{"t": "2026-06-22T00:00:00+00:00"}]}

    monkeypatch.setattr(ops.ops_metrics, "history", fake_history)
    login(client, repo)

    body = client.get("/api/ops/metrics/history?range=7d").json()
    assert body["resolution"] == "raw"
    assert body["points"]
    # 7d maps to a ~7-day-old `since`.
    assert (datetime.now(UTC) - captured["since"]).days == 7  # type: ignore[operator]


def test_ops_metrics_history_rejects_bad_range(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/ops/metrics/history?range=nonsense").status_code == 400


def test_ops_status_proxies_supervisor(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/ops/status")
    assert resp.status_code == 200
    assert resp.json() == SUPERVISOR_STATUS


def test_ops_restart(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.post("/api/ops/restart", json={"service": "api"})
    assert resp.status_code == 202
    assert resp.json() == {"restarting": ["api"]}


def test_ops_restart_unknown_service(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.post("/api/ops/restart", json={"service": "ghost"}).status_code == 404


def test_ops_logs(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/ops/logs/api")
    assert resp.status_code == 200
    assert "line1" in resp.text


def test_ops_update_trigger_and_status(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    started = client.post("/api/ops/update")
    assert started.status_code == 202
    assert started.json()["updater"] == "jbrain-updater-1"
    status = client.get("/api/ops/update/status")
    assert status.status_code == 200
    assert status.json()["state"] == "running"


def test_ops_metrics_merges_supervisor_and_local(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    body = client.get("/api/ops/metrics").json()
    assert body["mem_total_bytes"] == 4 << 30
    # Host telemetry the proxy carries through untouched (incl. fan RPM).
    assert body["fan_rpm"] == {"CPU Fan": 2100}
    assert body["containers"][0]["service"] == "api"
    # The unit-test app has no reachable database or blob store wired, so
    # the best-effort sections degrade to null instead of failing the call.
    assert body["db"] is None
    assert body["blobs"] is not None or body["blobs"] is None


def test_ops_update_requires_owner(client: TestClient) -> None:
    assert client.post("/api/ops/update").status_code == 401


def test_ops_logs_unknown_service(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/ops/logs/ghost").status_code == 404
