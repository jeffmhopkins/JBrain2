"""External-source corpus against real Postgres + pgvector: the two tables' RLS firewall,
and the persist -> embed -> search_corpus round-trip through the purpose-built read scope.

Embedding vectors are deterministic fakes (the embed container never runs in tests).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.embed import ExternalSourceEmbedder, vector_literal
from jbrain.external.corpus import (
    delete_external_video,
    fetch_transcript,
    filter_new_video_ids,
    list_corpus,
    persist_analysis,
    search_corpus,
)
from jbrain.ingest.video import VideoAnalysis
from jbrain.stream import ResolvedStream
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
EXTERNAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("external",))
DIMS = 384


def vec(*head: float) -> list[float]:
    v = [0.0] * DIMS
    for i, x in enumerate(head):
        v[i] = x
    return v


class StaticEmbed:
    """Deterministic embed fake: every text maps to one fixed vector."""

    def __init__(self, vector: list[float] | None = None, fail: bool = False):
        self.vector = vector or vec(1.0)
        self.fail = fail

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise ConnectionError("embed container down")
        return [self.vector for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _insert_source(maker, ctx, domain: str) -> str:
    async with scoped_session(maker, ctx) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.external_sources"
                        " (provider, video_id, url, summary, status, domain_code)"
                        " VALUES ('youtube', :vid, 'https://y', 'a summary', 'done', :dom)"
                        " RETURNING id"
                    ),
                    {"vid": f"v-{domain}-{uuid.uuid4().hex[:8]}", "dom": domain},
                )
            ).scalar_one()
        )


# --- RLS firewall (CLAUDE.md rule 3: an isolation test per new domain-scoped table) ----


async def test_external_sources_domain_firewall(maker) -> None:  # noqa: F811
    await _insert_source(maker, OWNER, "general")
    await _insert_source(maker, OWNER, "health")

    async with scoped_session(maker, GENERAL_ONLY) as s:
        assert (
            await s.execute(text("SELECT domain_code FROM app.external_sources ORDER BY 1"))
        ).scalars().all() == ["general"]
    async with scoped_session(maker, HEALTH_ONLY) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.external_sources"))).scalar() == 1
    async with scoped_session(maker, UNSCOPED) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.external_sources"))).scalar() == 0
    async with scoped_session(maker, OWNER) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.external_sources"))).scalar() == 2
    # A general-scoped writer cannot smuggle a health row past WITH CHECK.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.external_sources (provider, video_id, url, domain_code)"
                    " VALUES ('youtube', 'sneaky', 'https://y', 'health')"
                )
            )


async def test_external_source_chunks_domain_firewall(maker) -> None:  # noqa: F811
    gen = await _insert_source(maker, OWNER, "general")
    hlth = await _insert_source(maker, OWNER, "health")
    async with scoped_session(maker, OWNER) as s:
        for sid, dom in ((gen, "general"), (hlth, "health")):
            await s.execute(
                text(
                    "INSERT INTO app.external_source_chunks"
                    " (source_id, seq, t_ms, text, domain_code)"
                    " VALUES (:sid, 0, 0, 'a passage', :dom)"
                ),
                {"sid": sid, "dom": dom},
            )

    async with scoped_session(maker, GENERAL_ONLY) as s:
        assert (
            await s.execute(text("SELECT domain_code FROM app.external_source_chunks"))
        ).scalars().all() == ["general"]
    async with scoped_session(maker, UNSCOPED) as s:
        assert (
            await s.execute(text("SELECT count(*) FROM app.external_source_chunks"))
        ).scalar() == 0
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.external_source_chunks"
                    " (source_id, seq, t_ms, text, domain_code)"
                    " VALUES (:sid, 1, 0, 'x', 'health')"
                ),
                {"sid": hlth},
            )


# --- persist -> embed -> search round-trip + scope isolation ---------------------------


def _synthetic() -> tuple[ResolvedStream, VideoAnalysis]:
    resolved = ResolvedStream(
        media_url="https://media",
        title="Booster Rollout",
        is_live=False,
        duration_s=20.0,
        webpage_url="https://www.youtube.com/watch?v=vid1",
        provider="youtube",
        video_id="vid1",
        channel_name="NSF",
        upload_date="20260715",
        description="Chapters and links from the uploader about the Starbase rollout.",
    )
    analysis = {
        "duration_ms": 20_000,
        "frames": [
            {"t_ms": 5_000, "caption": "A booster rolls to the launch pad.", "thumb_id": "t"}
        ],
        "transcript": {
            "words": [
                {"text": "The", "start_ms": 6_000, "end_ms": 6_200, "confidence": 0.9},
                {"text": "booster", "start_ms": 6_200, "end_ms": 6_600, "confidence": 0.9},
                {"text": "is", "start_ms": 6_600, "end_ms": 6_800, "confidence": 0.9},
                {"text": "stacked.", "start_ms": 6_800, "end_ms": 7_200, "confidence": 0.9},
            ]
        },
    }
    return resolved, VideoAnalysis(
        summary="A booster rollout at the pad.", analysis=analysis, tool="t"
    )


async def test_persist_embed_search_round_trip(maker) -> None:  # noqa: F811
    resolved, result = _synthetic()
    source_id = await persist_analysis(
        maker, resolved=resolved, result=result, transcript_source="captions"
    )
    assert source_id is not None

    # The write-through lands the row in the corpus's own `external` domain (0136), so it's
    # firewalled from `general` owner knowledge: an external scope sees it, general does not.
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT domain_code FROM app.external_sources WHERE id = :s"), {"s": source_id}
            )
        ).scalar_one() == "external"
    async with scoped_session(maker, EXTERNAL_ONLY) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.external_sources WHERE id = :s"), {"s": source_id}
            )
        ).scalar_one() == 1
    async with scoped_session(maker, GENERAL_ONLY) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.external_sources WHERE id = :s"), {"s": source_id}
            )
        ).scalar_one() == 0

    # Passages were built and are FTS-visible immediately (embeddings fill next).
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.external_source_chunks WHERE source_id = :s"),
                {"s": source_id},
            )
        ).scalar_one() >= 1

    embed = StaticEmbed()
    await ExternalSourceEmbedder(maker, embed, "test-model").embed_external_source(
        {"source_id": source_id}
    )

    hits, degraded = await search_corpus(maker, embed, "booster", 6)
    assert not degraded
    assert [h.source_id for h in hits] == [source_id]
    hit = hits[0]
    assert hit.title == "Booster Rollout" and hit.channel_name == "NSF"
    assert hit.t_ms is not None  # a chunk hit deep-links to the moment

    # Degraded (embed down) still answers via the keyword leg.
    down_hits, down_degraded = await search_corpus(maker, StaticEmbed(fail=True), "booster", 6)
    assert down_degraded and [h.source_id for h in down_hits] == [source_id]

    # The full-read path returns metadata + every ordered passage (no embeddings needed).
    t = await fetch_transcript(maker, "vid1")
    assert t is not None
    assert t.title == "Booster Rollout" and t.channel_name == "NSF"
    assert t.duration_s == 20 and t.summary == "A booster rollout at the pad."
    # The uploader's own description round-trips for read_external_video to surface.
    assert t.description == "Chapters and links from the uploader about the Starbase rollout."
    assert t.published_at is not None and t.published_at.year == 2026  # upload_date "20260715"
    # The video-analysis card fields (show_external_video) round-trip too.
    assert t.video_id == "vid1" and t.provider == "youtube"
    assert t.duration_ms == 20_000 and t.frames and t.frames[0]["caption"]
    # The word-level transcript (0135) is stored + read back for the synced card tab.
    assert t.cued_transcript is not None and t.cued_transcript["words"]
    assert t.cued_transcript["words"][0]["text"] == "The"
    assert [w[1] for w in t.windows]  # passage windows came back, ordered by seq
    assert t.windows == sorted(t.windows)  # ascending by t_ms
    assert await fetch_transcript(maker, "nope") is None  # unknown id → None


async def test_search_finds_video_by_description_alone(maker) -> None:  # noqa: F811
    """A source with no summary and no transcript passages is still findable by its uploader
    description — the description-dense leg makes the channel-authored text searchable."""
    await _clear_sources(maker)
    async with scoped_session(maker, OWNER) as s:
        source_id = str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.external_sources"
                        " (provider, video_id, url, description, status)"
                        " VALUES ('youtube', 'descvid', 'https://y/descvid',"
                        " 'A deep dive on grid fin actuator qualification.', 'done')"
                        " RETURNING id"
                    )
                )
            ).scalar_one()
        )

    embed = StaticEmbed()
    await ExternalSourceEmbedder(maker, embed, "test-model").embed_external_source(
        {"source_id": source_id}
    )
    # The description embedding was filled (there was no summary and no chunk to embed).
    async with scoped_session(maker, EXTERNAL_ONLY) as s:
        assert (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.external_sources"
                    " WHERE id = :s AND description_embedding IS NOT NULL"
                ),
                {"s": source_id},
            )
        ).scalar_one() == 1

    # No summary, no passages → only the description leg can surface it.
    hits, degraded = await search_corpus(maker, embed, "grid fin actuator", 6)
    assert not degraded
    assert [h.source_id for h in hits] == [source_id]
    assert hits[0].passage == "A deep dive on grid fin actuator qualification."


async def _clear_sources(maker) -> None:
    # The module shares one database across tests, so start from a known-empty corpus
    # rather than assuming order (chunks cascade on the delete).
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.external_sources"))


async def test_list_corpus_counts_and_pages(maker) -> None:  # noqa: F811
    await _clear_sources(maker)
    # An empty library reports zero and returns no rows.
    videos, total = await list_corpus(maker, limit=10)
    assert total == 0 and videos == []

    # Three done sources with distinct analyzed_at, plus an in-flight one that must NOT count.
    async with scoped_session(maker, OWNER) as s:
        for i, day in enumerate((10, 12, 15)):
            await s.execute(
                text(
                    "INSERT INTO app.external_sources"
                    " (provider, video_id, url, title, status, analyzed_at)"
                    " VALUES ('youtube', :vid, 'https://y', :title, 'done', :ts)"
                ),
                {
                    "vid": f"done{i}",
                    "title": f"Video {i}",
                    "ts": datetime(2026, 7, day, tzinfo=UTC),
                },
            )
        await s.execute(
            text(
                "INSERT INTO app.external_sources (provider, video_id, url, title, status)"
                " VALUES ('youtube', 'wip', 'https://y', 'Analysing', 'analyzing')"
            )
        )

    # Total counts only the `done` library; newest analysis is listed first.
    page, total = await list_corpus(maker, limit=2)
    assert total == 3
    assert [v.title for v in page] == ["Video 2", "Video 1"]  # 07-15, 07-12
    assert page[0].video_id == "done2"

    # The second page picks up the remainder.
    page2, total2 = await list_corpus(maker, limit=2, offset=2)
    assert total2 == 3 and [v.title for v in page2] == ["Video 0"]

    # An offset past the end returns the true total but no rows.
    empty, total3 = await list_corpus(maker, limit=2, offset=99)
    assert total3 == 3 and empty == []


async def test_list_corpus_scope_excludes_health(maker) -> None:  # noqa: F811
    # A health-domain source is firewalled out of the tool's external-only read scope, so it
    # never inflates the "how many videos" count.
    await _clear_sources(maker)
    await _insert_source(maker, OWNER, "health")
    _, total = await list_corpus(maker, limit=10)
    assert total == 0


async def test_search_scope_excludes_health_corpus(maker) -> None:  # noqa: F811
    # A health-domain corpus row (with embeddings) must never surface through the tool's
    # general-only read scope, even on a matching query.
    async with scoped_session(maker, OWNER) as s:
        hid = str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.external_sources (provider, video_id, url, summary,"
                        " summary_embedding, embedding_model, status, domain_code)"
                        " VALUES ('youtube','h1','https://y','booster', cast(:e AS vector),"
                        " 'm', 'done', 'health') RETURNING id"
                    ),
                    {"e": vector_literal(vec(1.0))},
                )
            ).scalar_one()
        )
        await s.execute(
            text(
                "INSERT INTO app.external_source_chunks (source_id, seq, t_ms, text,"
                " embedding, embedding_model, domain_code)"
                " VALUES (:sid, 0, 0, 'a booster passage', cast(:e AS vector), 'm', 'health')"
            ),
            {"sid": hid, "e": vector_literal(vec(1.0))},
        )

    hits, _ = await search_corpus(maker, StaticEmbed(), "booster", 6)
    assert all(h.source_id != hid for h in hits)


async def test_filter_new_video_ids_skips_ingested(maker) -> None:  # noqa: F811
    # An already-ingested video is not "new"; check_channel uses this to avoid re-analysis.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.external_sources (provider, video_id, url, status)"
                " VALUES ('youtube', 'known1', 'https://y', 'done')"
            )
        )
    fresh = await filter_new_video_ids(maker, "youtube", ["known1", "new2", "new3"])
    assert fresh == {"new2", "new3"}
    assert await filter_new_video_ids(maker, "youtube", []) == set()


async def test_delete_external_video_removes_row_and_cascades(maker) -> None:  # noqa: F811
    # The removal-proposal executor's effect: hard-delete one source; chunks cascade (0134).
    resolved, result = _synthetic()
    source_id = await persist_analysis(
        maker, resolved=resolved, result=result, transcript_source="captions"
    )
    assert source_id is not None
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.external_source_chunks WHERE source_id = :s"),
                {"s": source_id},
            )
        ).scalar_one() >= 1

    assert await delete_external_video(maker, OWNER, source_id) is True
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.external_sources WHERE id = :s"), {"s": source_id}
            )
        ).scalar_one() == 0
        assert (  # chunks cascaded
            await s.execute(
                text("SELECT count(*) FROM app.external_source_chunks WHERE source_id = :s"),
                {"s": source_id},
            )
        ).scalar_one() == 0
    # Idempotent: deleting an already-gone video is a harmless no-op.
    assert await delete_external_video(maker, OWNER, source_id) is False
