"""The emergency-trigger Ops control (POST /ops/triggers/{id}/run): owner-only,
fires a manual trigger now and returns the enqueued job ids. The fire itself
(resolution + enqueue) is unit-tested in test_scheduler.py and integration-tested
against Postgres; here the DB is faked so the test exercises the HTTP surface —
auth, the job-id response, and the 404 for an unfireable trigger."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.workflow import scheduler
from jbrain.workflow.scheduler import FiredTrigger
from tests.unit.fakes import FakeAuthRepo


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


def _settings() -> Settings:
    return Settings(
        secure_cookies=False,
        supervisor_token="st-token",
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
    )


@pytest.fixture
def client(repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = create_app(_settings())
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        # The endpoint reads session_maker + action_registry off app.state; the
        # faked fire_trigger never touches either, so a sentinel is enough.
        app.state.session_maker = object()
        app.state.action_registry = object()
        login(test_client, repo)
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert (
        client.post(
            "/api/auth/session", json={"owner_key": key, "device_label": "test"}
        ).status_code
        == 204
    )


def test_run_trigger_requires_owner(repo: FakeAuthRepo) -> None:
    app = create_app(_settings())
    with TestClient(app) as anon:
        app.state.auth_repo = repo
        assert anon.post("/api/ops/triggers/trig-1/run").status_code == 401


def test_run_trigger_fires_and_returns_job_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def fake_fire(
        maker: object, registry: object, trigger_id: str, *, require_manual: bool = False
    ) -> FiredTrigger:
        captured["trigger_id"] = trigger_id
        return FiredTrigger(
            trigger_id=trigger_id, pipeline="nightly_sync", job_ids=["job-1", "job-2"]
        )

    monkeypatch.setattr(scheduler, "fire_trigger", fake_fire)

    resp = client.post("/api/ops/triggers/trig-7/run")
    assert resp.status_code == 202
    body = resp.json()
    assert body == {
        "trigger_id": "trig-7",
        "pipeline": "nightly_sync",
        "job_ids": ["job-1", "job-2"],
    }
    assert captured["trigger_id"] == "trig-7"


def test_run_trigger_404s_on_unfireable_trigger(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fire(
        maker: object, registry: object, trigger_id: str, *, require_manual: bool = False
    ) -> FiredTrigger:
        raise scheduler.ScheduleResolutionError(f"no trigger {trigger_id!r}")

    monkeypatch.setattr(scheduler, "fire_trigger", fake_fire)

    resp = client.post("/api/ops/triggers/ghost/run")
    assert resp.status_code == 404
    assert "ghost" in resp.json()["detail"]
