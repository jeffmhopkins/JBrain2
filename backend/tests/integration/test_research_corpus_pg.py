"""Research-report corpus against real Postgres + pgvector: the table's RLS firewall, and the
persist -> embed -> search / read / delete round-trip through the purpose-built `external` scope.

Embedding vectors are deterministic fakes (the embed container never runs in tests).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
from jbrain.external.report_titler import ResearchReportTitler
from jbrain.external.research_corpus import (
    delete_report,
    expire_reports,
    fetch_report,
    keep_report,
    list_reports,
    list_reports_for_session,
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


async def _expires_at(maker, report_id: str):  # noqa: ANN001, ANN202
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT expires_at FROM app.research_reports WHERE id = :r"), {"r": report_id}
            )
        ).scalar_one()


async def test_retention_stamps_and_refreshes_expires_at(maker) -> None:  # noqa: F811
    """A retention-bearing persist stamps expires_at ≈ now + N days; no retention → NULL (keep
    forever); a re-run refreshes the TTL off the new now() (REPORT_EXPIRY_PLAN.md, W1)."""
    await _clear_reports(maker)

    # No retention → NULL expires_at (the keep-forever default, every existing row).
    keep_id = await persist_report(
        maker,
        session_id=None,
        question="a report to keep forever",
        report_md="## Body\n\ntext",
        complexity="deep",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )
    assert await _expires_at(maker, keep_id) is None

    # retention_days=7 → expires_at set ~7 days out (server clock; assert a generous window).
    ttl_id = await persist_report(
        maker,
        session_id=None,
        question="a daily briefing that expires",
        report_md="## Body\n\ntext",
        complexity="brief",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
        retention_days=7,
    )
    first = await _expires_at(maker, ttl_id)
    assert first is not None
    async with scoped_session(maker, OWNER) as s:
        # 6–8 day window absorbs any test-clock skew while proving it's ~7, not 1 or 30.
        within = (
            await s.execute(
                text("SELECT :e BETWEEN now() + interval '6 days' AND now() + interval '8 days'"),
                {"e": first},
            )
        ).scalar_one()
    assert within is True

    # A re-run of the SAME question (dedup upsert) refreshes expires_at off the new now().
    again = await persist_report(
        maker,
        session_id=None,
        question="a daily briefing that expires",
        report_md="## Body\n\nrevised",
        complexity="brief",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
        retention_days=7,
    )
    assert again == ttl_id
    assert (await _expires_at(maker, ttl_id)) >= first  # rolled forward (or equal within a tick)


async def test_expire_reports_reaps_only_past_ttl(maker) -> None:  # noqa: F811
    """The sweep hard-deletes reports whose expires_at has passed, leaves keep-forever (NULL)
    and not-yet-due rows, is idempotent, and (running under the default SYSTEM_CTX) can delete
    across the corpus `external` scope (REPORT_EXPIRY_PLAN.md, W2)."""
    await _clear_reports(maker)
    keep_id = await persist_report(
        maker,
        session_id=None,
        question="keep me forever",
        report_md="## Body\n\ntext",
        complexity="deep",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )
    ttl_id = await persist_report(
        maker,
        session_id=None,
        question="expire me in a week",
        report_md="## Body\n\ntext",
        complexity="brief",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
        retention_days=7,
    )

    # Before the TTL is due, the sweep reaps nothing — the frozen `now` drives the cutoff.
    assert await expire_reports(maker, now=datetime.now(UTC)) == 0
    assert await fetch_report(maker, ttl_id) is not None

    # Eight days on, the TTL row is reaped; the keep-forever row is untouched.
    reaped = await expire_reports(maker, now=datetime.now(UTC) + timedelta(days=8))
    assert reaped == 1
    assert await fetch_report(maker, ttl_id) is None
    assert await fetch_report(maker, keep_id) is not None

    # Idempotent: a second sweep at the same instant deletes nothing more.
    assert await expire_reports(maker, now=datetime.now(UTC) + timedelta(days=8)) == 0


async def test_keep_report_clears_expiry_and_survives_the_sweep(maker) -> None:  # noqa: F811
    """keep_report nulls a report's TTL so the sweep no longer reaps it, and the cleared
    expiry surfaces in the listing (REPORT_EXPIRY_PLAN.md, W4)."""
    await _clear_reports(maker)
    rid = await persist_report(
        maker,
        session_id=None,
        question="a briefing the owner wants to keep",
        report_md="## Body\n\ntext",
        complexity="brief",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
        retention_days=7,
    )
    assert await _expires_at(maker, rid) is not None
    # The listing carries the expiry so the UI can show "expires in N days".
    reports, _ = await list_reports(maker, limit=10)
    assert reports[0].expires_at is not None

    # Keep it (owner ctx) → expiry cleared; idempotent; and now the far-future sweep spares it.
    assert await keep_report(maker, OWNER, rid) is True
    assert await _expires_at(maker, rid) is None
    assert await keep_report(maker, OWNER, rid) is True  # idempotent (already kept)
    reports2, _ = await list_reports(maker, limit=10)
    assert reports2[0].expires_at is None
    assert await expire_reports(maker, now=datetime.now(UTC) + timedelta(days=999)) == 0
    assert await fetch_report(maker, rid) is not None


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
    # The display title is None until the title_research_report job fills it.
    assert all(r.title is None for r in page1 + page2)


async def test_list_reports_for_session_scopes_to_that_session(maker) -> None:  # noqa: F811
    """The cross-turn reference read returns ONLY the reports produced in the given session,
    newest first, with each report's source count — so jerv's per-turn pointer names what it
    made in THIS chat (and a bad/non-uuid session id is a clean empty list, never an error)."""
    await _clear_reports(maker)
    sess_a, sess_b = str(uuid.uuid4()), str(uuid.uuid4())
    for i in range(2):
        await persist_report(
            maker,
            session_id=sess_a,
            question=f"session A question {i}",
            report_md=f"body A{i}",
            complexity="deep",
            rounds=1,
            sub_agents=2,
            analyzed=True,
            revised=True,
            coverage_limited=False,
            truncated=False,
            sources=[{"url": f"https://ex.com/{i}", "title": f"S{i}"}],
        )
    await persist_report(
        maker,
        session_id=sess_b,
        question="session B question",
        report_md="body B",
        complexity="deep",
        rounds=1,
        sub_agents=1,
        analyzed=True,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )
    refs = await list_reports_for_session(maker, session_id=sess_a, limit=5)
    # Newest first, and only this session's reports.
    assert [r.question for r in refs] == ["session A question 1", "session A question 0"]
    assert all(r.n_sources == 1 and r.tool == "deep_research" for r in refs)
    # Session B's single report is not mixed in, and its source count is honest (0).
    only_b = await list_reports_for_session(maker, session_id=sess_b, limit=5)
    assert [r.question for r in only_b] == ["session B question"] and only_b[0].n_sources == 0
    # A non-uuid id (or a session that produced nothing) is a clean empty list.
    assert await list_reports_for_session(maker, session_id="not-a-uuid", limit=5) == []
    assert await list_reports_for_session(maker, session_id=str(uuid.uuid4()), limit=5) == []
    # The `limit` bound truncates to the newest rows: a third report in session A, listed with
    # limit=2, yields the two newest only (never the oldest).
    await persist_report(
        maker,
        session_id=sess_a,
        question="session A question 2",
        report_md="body A2",
        complexity="deep",
        rounds=1,
        sub_agents=1,
        analyzed=True,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )
    limited = await list_reports_for_session(maker, session_id=sess_a, limit=2)
    assert [r.question for r in limited] == ["session A question 2", "session A question 1"]


class _FakeTitleRouter:
    """A one-shot completion router that returns a scripted title and records each call —
    the only LLM a test may call (the real router never runs in tests)."""

    def __init__(self, reply: str):
        self._reply = reply
        self.calls: list[dict] = []

    async def complete(self, task, *, system, user_text, max_tokens, spec_override=None, **_):  # noqa: ANN001
        self.calls.append({"task": task, "user_text": user_text, "spec_override": spec_override})
        return SimpleNamespace(text=self._reply, parsed=None)


async def test_persist_enqueues_title_job_and_titler_fills_it(maker) -> None:  # noqa: F811
    await _clear_reports(maker)
    report_id = await persist_report(
        maker,
        session_id=None,
        question="How was the 1918 flu pandemic's death toll estimated?",
        report_md="## Toll\n\nEstimates rest on excess-mortality models.",
        complexity="deep",
        rounds=1,
        sub_agents=2,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )

    # persist_report kicks BOTH follow-up jobs for the row (embed + title).
    async with scoped_session(maker, OWNER) as s:
        kinds = set(
            (
                await s.execute(
                    text("SELECT kind FROM app.jobs WHERE payload->>'report_id' = :r"),
                    {"r": report_id},
                )
            ).scalars()
        )
    assert {"embed_research_report", "title_research_report"} <= kinds

    router = _FakeTitleRouter('  "1918 Flu Death-Toll Estimates"  ')
    await ResearchReportTitler(maker, router).title_research_report(  # type: ignore[arg-type]
        {"report_id": report_id}
    )
    # The raw question (and the stored excerpt) were fed to the model.
    assert "1918 flu" in router.calls[0]["user_text"]
    # The reply is cleaned (quotes + whitespace stripped) and surfaces in the listing.
    reports, _ = await list_reports(maker, limit=10)
    assert reports[0].title == "1918 Flu Death-Toll Estimates"

    # Idempotent: a second run sees title IS NOT NULL and never calls the model again.
    router2 = _FakeTitleRouter("A Different Title")
    await ResearchReportTitler(maker, router2).title_research_report(  # type: ignore[arg-type]
        {"report_id": report_id}
    )
    assert router2.calls == []
    reports2, _ = await list_reports(maker, limit=10)
    assert reports2[0].title == "1918 Flu Death-Toll Estimates"


async def test_title_job_carries_and_uses_the_per_conversation_model(maker) -> None:  # noqa: F811
    """A run pinned to an omnibox model titles its report on that SAME model: persist_report
    stamps the pick onto the title job payload, and the titler routes the completion with it as
    spec_override (mirroring the session titler). No pin → no spec_override (default route)."""
    await _clear_reports(maker)
    report_id = await persist_report(
        maker,
        session_id=None,
        question="What drives the price of vanadium redox-flow batteries?",
        report_md="## Cost\n\nElectrolyte vanadium dominates the bill of materials.",
        complexity="deep",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
        model_spec="local:qwen3.8-27b-q4",
    )

    # The per-conversation model rides the title job's payload, not a schema column.
    async with scoped_session(maker, OWNER) as s:
        spec = (
            await s.execute(
                text(
                    "SELECT payload->>'model_spec' FROM app.jobs"
                    " WHERE kind = 'title_research_report' AND payload->>'report_id' = :r"
                ),
                {"r": report_id},
            )
        ).scalar_one()
    assert spec == "local:qwen3.8-27b-q4"

    # The titler forwards that model to the router as the per-call spec_override.
    router = _FakeTitleRouter("Vanadium Flow-Battery Cost Drivers")
    await ResearchReportTitler(maker, router).title_research_report(  # type: ignore[arg-type]
        {"report_id": report_id, "model_spec": "local:qwen3.8-27b-q4"}
    )
    assert router.calls[0]["spec_override"] == "local:qwen3.8-27b-q4"

    # A report with no pinned model enqueues no model_spec and titles on the default route.
    plain_id = await persist_report(
        maker,
        session_id=None,
        question="How do tidal barrages compare to tidal-stream turbines?",
        report_md="## Comparison\n\nBarrages front-load capital; streams scale incrementally.",
        complexity="deep",
        rounds=1,
        sub_agents=1,
        analyzed=False,
        revised=False,
        coverage_limited=False,
        truncated=False,
        sources=[],
    )
    async with scoped_session(maker, OWNER) as s:
        has_spec = (
            await s.execute(
                text(
                    "SELECT payload ? 'model_spec' FROM app.jobs"
                    " WHERE kind = 'title_research_report' AND payload->>'report_id' = :r"
                ),
                {"r": plain_id},
            )
        ).scalar_one()
    assert has_spec is False
    router2 = _FakeTitleRouter("Tidal Barrages vs Stream Turbines")
    await ResearchReportTitler(maker, router2).title_research_report(  # type: ignore[arg-type]
        {"report_id": plain_id}
    )
    assert router2.calls[0]["spec_override"] is None
