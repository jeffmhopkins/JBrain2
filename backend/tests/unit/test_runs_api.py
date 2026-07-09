"""The Runs API with a fake reader on app.state — owner-only list + step tree."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.runlog import RunDetail, RunStats, RunStepView, RunSummary
from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

STARTED = datetime(2026, 6, 15, 2, 0, tzinfo=UTC)


class FakeRunReader:
    def __init__(self) -> None:
        self._summaries = [
            RunSummary(
                id="r3",
                kind="integration",
                status="error",
                name="integrate_note",
                started_at=STARTED,
                duration_ms=31000,
                step_count=4,
                cost_tokens=6700,
                last_error="ocr_attachment · labs.pdf",
                progress_note=None,
            ),
            RunSummary(
                id="r1",
                kind="agent",
                status="running",
                name="agent",
                started_at=STARTED,
                duration_ms=None,
                step_count=3,
                cost_tokens=4100,
                last_error=None,
                progress_note="processed 12 of 30 emails",
            ),
        ]
        self._detail = RunDetail(
            id="r3",
            kind="integration",
            status="error",
            name="integrate_note",
            started_at=STARTED,
            duration_ms=31000,
            step_count=4,
            cost_tokens=6700,
            stop_reason="step_error",
            progress_note=None,
            steps=[
                RunStepView(
                    idx=0,
                    kind="model",
                    name="classify domain",
                    ok=True,
                    cost_tokens=300,
                    job_id=None,
                    error=None,
                    detail=[{"event": "llm.complete", "task": "note.extract"}],
                ),
                RunStepView(
                    idx=1,
                    kind="job",
                    name="ocr_attachment · labs.pdf",
                    ok=False,
                    cost_tokens=1100,
                    job_id="job-7",
                    error="ocr_attachment · labs.pdf",
                    detail=None,
                ),
            ],
        )

        self.calls: dict[str, object] = {}

    async def list_recent(
        self,
        ctx: object,
        *,
        limit: int = 50,
        kinds: list[str] | None = None,
        exclude_sweeps: bool = False,
        since: datetime | None = None,
    ) -> list[RunSummary]:
        # Record the resolved filters so the API-layer test can assert they flow through.
        self.calls = {
            "limit": limit,
            "kinds": kinds,
            "exclude_sweeps": exclude_sweeps,
            "since": since,
        }
        return self._summaries

    async def stats(
        self, ctx: object, *, since: datetime | None = None, exclude_sweeps: bool = False
    ) -> RunStats:
        self.calls = {"stats_since": since, "stats_exclude_sweeps": exclude_sweeps}
        return RunStats(
            active=1,
            failed_today=2,
            tokens_today=41000,
            by_kind={"agent": 3, "integration": 1, "pipeline": 47},
        )

    async def load(self, ctx: object, run_id: str) -> RunDetail | None:
        return self._detail if run_id == "r3" else None

    async def queue_depth(self, ctx: object) -> int:
        return 5


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def reader() -> FakeRunReader:
    return FakeRunReader()


@pytest.fixture
def client(repo: FakeAuthRepo, reader: FakeRunReader) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.run_reader = reader
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_runs_require_owner(client: TestClient) -> None:
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/runs/r3").status_code == 401
    assert client.get("/api/runs/queue-depth").status_code == 401


def test_queue_depth(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/runs/queue-depth")
    assert resp.status_code == 200
    assert resp.json() == {"queued": 5}


def test_list_runs_shape(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body] == ["r3", "r1"]
    failed = body[0]
    # The API passes the stored status through ('error'); the client renders it
    # as the "failed" tile/dot.
    assert failed["status"] == "error"
    assert failed["last_error"] == "ocr_attachment · labs.pdf"
    # A running run reports no honest duration yet, but carries its live progress note.
    assert body[1]["duration_ms"] is None
    assert body[1]["progress_note"] == "processed 12 of 30 emails"
    assert failed["progress_note"] is None


def test_run_detail_step_tree(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/runs/r3")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["stop_reason"] == "step_error"
    steps = detail["steps"]
    assert [s["idx"] for s in steps] == [0, 1]
    assert steps[0]["ok"] is True
    assert steps[1]["ok"] is False
    assert steps[1]["job_id"] == "job-7"
    assert steps[1]["error"] == "ocr_attachment · labs.pdf"


def test_unknown_run_is_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/runs/ghost").status_code == 404


def test_list_runs_passes_filters_through(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    login(client, repo)
    resp = client.get(
        "/api/runs",
        params={
            "kinds": ["agent", "subagent"],
            "exclude_sweeps": "true",
            "since": "2026-06-15T00:00:00Z",
            "limit": 25,
        },
    )
    assert resp.status_code == 200
    assert reader.calls["kinds"] == ["agent", "subagent"]
    assert reader.calls["exclude_sweeps"] is True
    assert reader.calls["limit"] == 25
    assert reader.calls["since"] == datetime(2026, 6, 15, tzinfo=UTC)


def test_list_runs_clamps_limit_to_max(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    login(client, repo)
    assert client.get("/api/runs", params={"limit": 100000}).status_code == 200
    assert reader.calls["limit"] == 200  # MAX_LIMIT, never an unbounded scan


def test_run_stats_shape(client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader) -> None:
    login(client, repo)
    resp = client.get("/api/runs/stats", params={"exclude_sweeps": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "active": 1,
        "failed_today": 2,
        "tokens_today": 41000,
        "by_kind": {"agent": 3, "integration": 1, "pipeline": 47},
    }
    # "/stats" wins the route match over "/{run_id}" (never parsed as a run id).
    assert reader.calls["stats_exclude_sweeps"] is True


def test_run_stats_requires_owner(client: TestClient) -> None:
    assert client.get("/api/runs/stats").status_code == 401
