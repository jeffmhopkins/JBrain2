"""The Research Library API with a fake reader on app.state — owner-only browse of the
deep-research report + analysed-video corpora (list / search / view / delete)."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.external.corpus import CorpusHit, ExternalTranscript, LibraryVideo
from jbrain.external.research_corpus import LibraryReport, ReportHit, ReportRecord
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

WHEN = datetime(2026, 7, 18, 2, 0, tzinfo=UTC)


class FakeResearchLibrary:
    """Mirrors `api.research_service.ResearchLibrary`; records calls so the API-layer test can
    assert filters/ids flow through and that delete runs under the owner context."""

    def __init__(self) -> None:
        self._report = ReportRecord(
            id="rep-1",
            question="How was the 1918 flu pandemic's death toll estimated?",
            report_md="## Summary\n\nEstimates range 17M–100M; the cited 50M is Johnson & Mueller.",
            complexity="deep",
            rounds=2,
            sub_agents=6,
            analyzed=True,
            revised=True,
            coverage_limited=False,
            truncated=False,
            sources=[{"url": "https://example.org/a", "title": "A"}],
            created_at=WHEN,
        )
        self._video = ExternalTranscript(
            source_id="src-1",
            title="Deep Research Agent locally on Strix Halo",
            channel_name="Donato Capitella",
            url="https://www.youtube.com/watch?v=strix",
            transcript_source="captions",
            summary="Runs a local deep-research agent on Strix Halo.",
            duration_s=1694,
            published_at=WHEN,
            windows=[(0, "intro"), (5000, "the orchestrator holds no web tools")],
            video_id="vid-1",
            provider="youtube",
            duration_ms=1694000,
            frames=[{"t_ms": 0, "caption": "title card", "thumb_id": "t0"}],
            cued_transcript=None,
        )
        self.calls: dict[str, object] = {}

    async def list_reports(
        self, principal_id: str, *, limit: int, offset: int
    ) -> tuple[list[LibraryReport], int]:
        self.calls["reports_limit"] = limit
        self.calls["reports_offset"] = offset
        return [
            LibraryReport(
                id="rep-1",
                question=self._report.question,
                complexity="deep",
                created_at=WHEN,
                sub_agents=6,
                rounds=2,
            )
        ], 3

    async def search_reports(
        self, principal_id: str, query: str, limit: int
    ) -> tuple[list[ReportHit], bool]:
        self.calls["reports_query"] = query
        return [ReportHit(id="rep-1", question=self._report.question, excerpt="flu toll…")], True

    async def fetch_report(self, principal_id: str, ref: str) -> ReportRecord | None:
        return self._report if ref == "rep-1" else None

    async def delete_report(self, ctx: SessionContext, report_id: str) -> bool:
        self.calls["deleted_report"] = (ctx, report_id)
        return report_id == "rep-1"

    async def list_videos(
        self, principal_id: str, *, limit: int, offset: int
    ) -> tuple[list[LibraryVideo], int]:
        self.calls["videos_limit"] = limit
        return [
            LibraryVideo(
                title=self._video.title,
                channel_name=self._video.channel_name,
                url=self._video.url,
                published_at=WHEN,
                duration_s=1694,
                video_id="vid-1",
                provider="youtube",
            )
        ], 2

    async def search_videos(
        self, principal_id: str, query: str, limit: int
    ) -> tuple[list[CorpusHit], bool]:
        self.calls["videos_query"] = query
        return [
            CorpusHit(
                source_id="src-1",
                title=self._video.title,
                channel_name=self._video.channel_name,
                url=self._video.url,
                passage="the orchestrator holds no web tools",
                t_ms=5000,
            )
        ], False

    async def fetch_video(self, principal_id: str, video_id: str) -> ExternalTranscript | None:
        return self._video if video_id == "vid-1" else None

    async def delete_video(self, ctx: SessionContext, source_id: str) -> bool:
        self.calls["deleted_video"] = (ctx, source_id)
        return source_id == "src-1"


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def library() -> FakeResearchLibrary:
    return FakeResearchLibrary()


@pytest.fixture
def client(repo: FakeAuthRepo, library: FakeResearchLibrary) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.research_library = library
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_all_routes_require_owner(client: TestClient) -> None:
    # No session cookie → the shared owner gate 401s every route before any read/delete.
    r = "/api/research-library"
    assert client.get(f"{r}/reports").status_code == 401
    assert client.get(f"{r}/reports/search", params={"q": "flu"}).status_code == 401
    assert client.get(f"{r}/reports/rep-1").status_code == 401
    assert client.delete(f"{r}/reports/rep-1").status_code == 401
    assert client.get(f"{r}/videos").status_code == 401
    assert client.get(f"{r}/videos/vid-1").status_code == 401
    assert client.delete(f"{r}/videos/vid-1").status_code == 401


def test_list_reports_shape_and_total(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/research-library/reports")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [r["id"] for r in body["items"]] == ["rep-1"]
    row = body["items"][0]
    assert row["complexity"] == "deep" and row["sub_agents"] == 6 and row["rounds"] == 2
    # The listing carries no body — that's the detail read's job.
    assert "report_md" not in row


def test_search_reports_passes_degraded_through(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/research-library/reports/search", params={"q": "flu toll"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True  # embed-down keyword-only signal reaches the client
    assert body["items"][0]["excerpt"] == "flu toll…"


def test_search_requires_query(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/research-library/reports/search").status_code == 422
    assert client.get("/api/research-library/reports/search", params={"q": ""}).status_code == 422


def test_get_report_full_and_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    ok = client.get("/api/research-library/reports/rep-1")
    assert ok.status_code == 200
    assert ok.json()["report_md"].startswith("## Summary")
    assert client.get("/api/research-library/reports/ghost").status_code == 404


def test_delete_report_204_under_owner_ctx(
    client: TestClient, repo: FakeAuthRepo, library: FakeResearchLibrary
) -> None:
    login(client, repo)
    assert client.delete("/api/research-library/reports/rep-1").status_code == 204
    ctx, rid = library.calls["deleted_report"]  # type: ignore[misc]
    assert rid == "rep-1"
    # A full-owner context (never a jerv-style narrowed scope) is the trusted executor.
    assert isinstance(ctx, SessionContext) and ctx.principal_kind == "owner"
    assert ctx.owner_scoped is False
    # Idempotent: an already-gone report still resolves 204.
    assert client.delete("/api/research-library/reports/ghost").status_code == 204


def test_list_videos_shape_and_total(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/research-library/videos")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    row = body["items"][0]
    assert row["video_id"] == "vid-1" and row["provider"] == "youtube"
    assert row["duration_s"] == 1694


def test_get_video_maps_windows_and_frames(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/research-library/videos/vid-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_id"] == "src-1"
    assert body["windows"] == [
        {"t_ms": 0, "text": "intro"},
        {"t_ms": 5000, "text": "the orchestrator holds no web tools"},
    ]
    assert body["frames"][0]["thumb_id"] == "t0"
    assert client.get("/api/research-library/videos/ghost").status_code == 404


def test_delete_video_resolves_source_id(
    client: TestClient, repo: FakeAuthRepo, library: FakeResearchLibrary
) -> None:
    login(client, repo)
    assert client.delete("/api/research-library/videos/vid-1").status_code == 204
    ctx, sid = library.calls["deleted_video"]  # type: ignore[misc]
    assert sid == "src-1"  # keyed by video_id on the wire, deleted by resolved row id
    assert isinstance(ctx, SessionContext) and ctx.principal_kind == "owner"
    # An unknown video resolves to nothing → 204 and no delete is attempted.
    library.calls.pop("deleted_video")
    assert client.delete("/api/research-library/videos/ghost").status_code == 204
    assert "deleted_video" not in library.calls


def test_list_clamps_limit_to_max(
    client: TestClient, repo: FakeAuthRepo, library: FakeResearchLibrary
) -> None:
    login(client, repo)
    assert client.get("/api/research-library/reports", params={"limit": 100000}).status_code == 200
    assert library.calls["reports_limit"] == 200  # MAX_LIMIT — never an unbounded scan
