"""API-surface tests with a fake repo and a mocked supervisor."""

import json
from collections.abc import Iterator

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


def test_ops_logs_unknown_service(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/ops/logs/ghost").status_code == 404
