"""The Research Library HTTP API end-to-end against real Postgres (build plan
docs/plans/RESEARCH_LIBRARY_PLAN.md, Wave R1).

Drives the actual FastAPI app: owner login, then list / view / delete for BOTH corpora
(deep-research reports + analysed videos) seeded directly into their `external`-domain
tables. Proves the owner-gated wrapper reaches real RLS-scoped rows and that a full-owner
delete actually removes them (the security path — an unauthenticated caller is refused).
Keyword search runs with the embed container down (the deterministic degraded path).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker: async_sessionmaker[AsyncSession], video_id: str) -> None:
    """One report + one analysed video, both in the default `external` domain (0136/0140),
    seeded under the owner context — the shape the corpus readers browse."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.research_reports"))
        await s.execute(text("DELETE FROM app.external_sources"))
        await s.execute(
            text(
                "INSERT INTO app.research_reports"
                " (question, question_hash, report_md, complexity, rounds, sub_agents, status)"
                " VALUES ('How did the 1918 flu toll get estimated?', :h,"
                " '## Summary\n\nEstimates range 17M-100M.', 'deep', 2, 6, 'done')"
            ),
            {"h": uuid.uuid4().hex},
        )
        await s.execute(
            text(
                "INSERT INTO app.external_sources"
                " (provider, video_id, url, title, channel_name, summary, transcript_source,"
                "  duration_s, status, analyzed_at)"
                " VALUES ('youtube', :vid, 'https://youtu.be/x', 'Strix Halo deep research',"
                "  'Donato Capitella', 'A local deep-research agent.', 'captions', 1694,"
                "  'done', now())"
            ),
            {"vid": video_id},
        )


async def test_research_library_api_round_trip(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    video_id = "vid-strix"
    await _seed(maker, video_id)
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        base = "/api/research-library"

        # Owner-gated: no session → 401 on every surface.
        assert client.get(f"{base}/reports").status_code == 401
        assert client.delete(f"{base}/videos/{video_id}").status_code == 401

        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204

        # --- reports: list → view → keyword search (embed down → degraded) → delete → 404 ---
        reports = client.get(f"{base}/reports").json()
        assert reports["total"] == 1
        report_id = reports["items"][0]["id"]
        assert reports["items"][0]["complexity"] == "deep"

        detail = client.get(f"{base}/reports/{report_id}")
        assert detail.status_code == 200
        assert detail.json()["report_md"].startswith("## Summary")

        found = client.get(f"{base}/reports/search", params={"q": "flu"})
        assert found.status_code == 200
        body = found.json()
        assert body["degraded"] is True  # no embed container in tests → keyword-only
        assert any(h["id"] == report_id for h in body["items"])

        assert client.delete(f"{base}/reports/{report_id}").status_code == 204
        assert client.get(f"{base}/reports/{report_id}").status_code == 404
        assert client.get(f"{base}/reports").json()["total"] == 0
        # A non-uuid id resolves to None first → a clean 204 no-op, never a 500 from
        # `cast(:id AS uuid)` against real Postgres (the resolve-before-delete guard).
        assert client.delete(f"{base}/reports/not-a-uuid").status_code == 204

        # --- videos: list → view (keyed by video_id) → delete → 404 ---
        videos = client.get(f"{base}/videos").json()
        assert videos["total"] == 1
        assert videos["items"][0]["video_id"] == video_id

        vdetail = client.get(f"{base}/videos/{video_id}")
        assert vdetail.status_code == 200
        assert vdetail.json()["title"] == "Strix Halo deep research"

        assert client.delete(f"{base}/videos/{video_id}").status_code == 204
        assert client.get(f"{base}/videos/{video_id}").status_code == 404
        assert client.get(f"{base}/videos").json()["total"] == 0
        # Idempotent: deleting the already-gone video is still a clean 204.
        assert client.delete(f"{base}/videos/{video_id}").status_code == 204
