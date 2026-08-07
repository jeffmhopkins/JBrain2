"""The Plan API is owner-only (JERV_PLANNING_TOOL_PLAN.md). The router carries
`Depends(owner_only)`, which rejects an unauthenticated / non-owner caller BEFORE any
handler or DB access — so these guard checks need no database. The owner happy-path (a
real approve/edit round-trip) is covered by the RLS integration test."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = FakeAuthRepo()
        yield test_client


def test_plan_endpoints_require_an_authenticated_owner(client: TestClient) -> None:
    # Every plan route — read AND the state-changing ones — 401s an unauthenticated
    # caller at the router dependency, before the handler runs. The `approve` gate (the
    # one transition jerv can't make itself) is the security-critical one.
    assert client.get("/api/plans/s-1").status_code == 401
    assert client.get("/api/plans/session/s-1/active").status_code == 401
    assert client.post("/api/plans/s-1/approve").status_code == 401
    assert client.post("/api/plans/s-1/edit", json={"body": "x"}).status_code == 401
    assert client.post("/api/plans/s-1/stop").status_code == 401
    assert client.post("/api/plans/s-1/continue").status_code == 401
