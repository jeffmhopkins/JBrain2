"""Research-report corpus against real Postgres + pgvector: the table's RLS firewall, and the
persist -> embed -> search / read / delete round-trip through the purpose-built `external` scope.

Embedding vectors are deterministic fakes (the embed container never runs in tests).
"""

import uuid
from collections.abc import AsyncIterator

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
from jbrain.embed import ResearchReportEmbedder
from jbrain.external.research_corpus import (
    delete_report,
    fetch_report,
    list_reports,
    persist_report,
    search_reports,
)
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


async def _insert_report(maker, ctx, domain: str) -> str:
    async with scoped_session(maker, ctx) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.research_reports"
                        " (question, question_hash, report_md, status, domain_code)"
                        " VALUES ('q', :h, 'a report', 'done', :dom) RETURNING id"
                    ),
                    {"h": uuid.uuid4().hex, "dom": domain},
                )
            ).scalar_one()
        )


# --- RLS firewall (CLAUDE.md rule 3: an isolation test per new domain-scoped table) ----


async def test_research_reports_domain_firewall(maker) -> None:  # noqa: F811
    await _insert_report(maker, OWNER, "general")
    await _insert_report(maker, OWNER, "health")

    async with scoped_session(maker, GENERAL_ONLY) as s:
        assert (
            await s.execute(text("SELECT domain_code FROM app.research_reports ORDER BY 1"))
        ).scalars().all() == ["general"]
    async with scoped_session(maker, HEALTH_ONLY) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.research_reports"))).scalar() == 1
    async with scoped_session(maker, UNSCOPED) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.research_reports"))).scalar() == 0
    async with scoped_session(maker, OWNER) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.research_reports"))).scalar() == 2
    # A general-scoped writer cannot smuggle a health row past WITH CHECK.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.research_reports"
                    " (question, question_hash, report_md, domain_code)"
                    " VALUES ('q', :h, 'r', 'health')"
                ),
                {"h": uuid.uuid4().hex},
            )


# --- persist -> embed -> search / read / delete round-trip + scope isolation ------------


async def _clear_reports(maker) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.research_reports"))


async def test_persist_embed_search_fetch_delete_round_trip(maker) -> None:  # noqa: F811
    await _clear_reports(maker)
    report_id = await persist_report(
        maker,
        session_id=None,
        question="How many people did the 1918 flu kill?",
        report_md="## Toll\n\nThe booster estimate is roughly 50 million deaths [^1].",
        complexity="deep",
        rounds=2,
        sub_agents=3,
        analyzed=True,
        revised=True,
        coverage_limited=False,
        truncated=False,
        sources=[{"url": "https://ex.com/a", "title": "A"}],
    )

    # The write lands in the corpus's own `external` domain (0140), firewalled from `general`.
    async with scoped_session(maker, OWNER) as s:
        assert (
            await s.execute(
                text("SELECT domain_code FROM app.research_reports WHERE id = :r"), {"r": report_id}
            )
        ).scalar_one() == "external"
    async with scoped_session(maker, GENERAL_ONLY) as s:
        assert (
            await s.execute(
                text("SELECT count(*) FROM app.research_reports WHERE id = :r"), {"r": report_id}
            )
        ).scalar_one() == 0

    embed = StaticEmbed()
    await ResearchReportEmbedder(maker, embed, "test-model").embed_research_report(
        {"report_id": report_id}
    )

    hits, degraded = await search_reports(maker, embed, "booster", 6)
    assert not degraded
    assert [h.id for h in hits] == [report_id]
    assert "1918 flu" in hits[0].question

    # Degraded (embed down) still answers via the keyword leg.
    down_hits, down_degraded = await search_reports(maker, StaticEmbed(fail=True), "booster", 6)
    assert down_degraded and [h.id for h in down_hits] == [report_id]

    # Full read: by id AND by the exact question (both resolve); an unknown ref is None.
    by_id = await fetch_report(maker, report_id)
    assert by_id is not None and by_id.report_md.startswith("## Toll")
    assert by_id.rounds == 2 and by_id.analyzed and by_id.sources[0]["url"] == "https://ex.com/a"
    by_q = await fetch_report(maker, "How many people did the 1918 flu kill?")
    assert by_q is not None and by_q.id == report_id
    assert await fetch_report(maker, str(uuid.uuid4())) is None

    # A re-run of the SAME question upserts in place (dedup on question_hash), not a new row.
    again = await persist_report(
        maker,
        session_id=None,
        question="How many people did the 1918 flu kill?",
        report_md="## Toll\n\nRevised: closer to 50 million booster deaths.",
        complexity="deep",
        rounds=1,
        sub_agents=2,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )
    assert again == report_id
    async with scoped_session(maker, OWNER) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.research_reports"))).scalar() == 1

    # Delete removes it (idempotent: a second delete is a no-op).
    assert await delete_report(maker, OWNER, report_id) is True
    assert await delete_report(maker, OWNER, report_id) is False
    assert await fetch_report(maker, report_id) is None


async def test_source_mode_round_trips(maker) -> None:  # noqa: F811
    """A library-scoped run persists its `source_mode`, and a fetch reads it back — so a
    re-shown/recalled report can badge where it came from. A row written without a mode
    (the legacy / default path) reads back as `web`."""
    await _clear_reports(maker)
    lib_id = await persist_report(
        maker,
        session_id=None,
        question="what do my videos say about eurorack?",
        report_md="## Modules\n\nThe library covers several oscillators.",
        complexity="deep",
        rounds=1,
        sub_agents=2,
        analyzed=True,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
        source_mode="library",
    )
    lib = await fetch_report(maker, lib_id)
    assert lib is not None and lib.source_mode == "library"

    # No mode passed → stored NULL → reads back as the legacy `web` default.
    web_id = await persist_report(
        maker,
        session_id=None,
        question="an ordinary web question",
        report_md="## Answer\n\nFrom the open web.",
        complexity="simple",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )
    web = await fetch_report(maker, web_id)
    assert web is not None and web.source_mode == "web"


async def test_list_reports_counts_and_pages(maker) -> None:  # noqa: F811
    await _clear_reports(maker)
    reports, total = await list_reports(maker, limit=10)
    assert total == 0 and reports == []

    for i in range(3):
        await persist_report(
            maker,
            session_id=None,
            question=f"question number {i}",
            report_md=f"report body {i}",
            complexity="deep",
            rounds=1,
            sub_agents=1,
            analyzed=False,
            revised=False,
            coverage_limited=False,
            truncated=False,
            sources=[],
        )
    page1, total = await list_reports(maker, limit=2, offset=0)
    assert total == 3 and len(page1) == 2
    page2, _ = await list_reports(maker, limit=2, offset=2)
    assert len(page2) == 1
    # Every report is enumerated exactly once across the pages.
    assert {r.question for r in page1 + page2} == {f"question number {i}" for i in range(3)}
