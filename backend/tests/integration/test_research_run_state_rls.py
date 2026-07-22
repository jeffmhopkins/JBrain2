"""Migration 0147 against real Postgres: app.research_run_state is the RLS `external`
checkpoint for a background deepest run (DEEPEST_RESEARCH_TOOL_PLAN.md, R5). A scoped
non-owner principal can neither read, write, nor claim a run's state (CLAUDE.md rule 3);
and the repo round-trips create → checkpoint → load → finish with the sticky terminal
status and the atomic exactly-once resume claim."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.external import research_run_state as rrs
from jbrain.external.research_run_state import run_state_context
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The owner reaches the run-state through the corpus `external` scope (like the report it
# produces); a scoped non-owner (general only) must see none of it.
OWNER_EXT = run_state_context(OWNER.principal_id)
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _run_id() -> str:
    return f"deepest-{uuid.uuid4()}"


async def _create(maker: async_sessionmaker, ctx: SessionContext, run_id: str) -> None:
    await rrs.create_run(
        maker,
        ctx,
        run_id=run_id,
        session_id=str(uuid.uuid4()),
        question="how does X actually work",
        ceiling_tokens=50_000_000,
        wall_clock_deadline=None,
    )


async def test_repo_round_trip_checkpoint_load_finish(maker: async_sessionmaker) -> None:
    rid = _run_id()
    await _create(maker, OWNER_EXT, rid)
    st = await rrs.load(maker, OWNER_EXT, rid)
    assert st is not None and st.status == "running" and st.round == 0 and st.state == {}

    ok = await rrs.checkpoint(
        maker,
        OWNER_EXT,
        run_id=rid,
        round=2,
        spent_tokens=12_000_000,
        agents_spawned=7,
        state={"sources": [{"url": "u1"}], "coverage": 0.6},
    )
    assert ok is True
    st = await rrs.load(maker, OWNER_EXT, rid)
    assert st is not None
    assert st.round == 2 and st.spent_tokens == 12_000_000 and st.agents_spawned == 7
    assert st.state["coverage"] == 0.6 and st.state["sources"] == [{"url": "u1"}]

    assert await rrs.finish(maker, OWNER_EXT, run_id=rid, status="done") is True
    st = await rrs.load(maker, OWNER_EXT, rid)
    assert st is not None and st.status == "done"
    # A checkpoint after the run finished is a no-op (guarded on status='running').
    assert (
        await rrs.checkpoint(
            maker, OWNER_EXT, run_id=rid, round=3, spent_tokens=1, agents_spawned=1, state={}
        )
        is False
    )


async def test_finish_is_sticky(maker: async_sessionmaker) -> None:
    rid = _run_id()
    await _create(maker, OWNER_EXT, rid)
    assert await rrs.finish(maker, OWNER_EXT, run_id=rid, status="cancelled") is True
    # A later terminal write can't override a terminal state — the first one wins.
    assert await rrs.finish(maker, OWNER_EXT, run_id=rid, status="done") is False
    st = await rrs.load(maker, OWNER_EXT, rid)
    assert st is not None and st.status == "cancelled"


async def test_claim_resume_is_exactly_once(maker: async_sessionmaker) -> None:
    rid = _run_id()
    await _create(maker, OWNER_EXT, rid)
    assert await rrs.claim_resume(maker, OWNER_EXT, rid) is True  # first claimant wins
    assert await rrs.claim_resume(maker, OWNER_EXT, rid) is False  # already claimed
    st = await rrs.load(maker, OWNER_EXT, rid)
    assert st is not None and st.resumed_at is not None


async def test_scoped_principal_cannot_read(maker: async_sessionmaker) -> None:
    rid = _run_id()
    await _create(maker, OWNER_EXT, rid)
    assert await rrs.load(maker, OWNER_EXT, rid) is not None  # owner sees it
    assert await rrs.load(maker, GENERAL_ONLY, rid) is None  # RLS hides it, not an error


async def test_scoped_principal_cannot_write(maker: async_sessionmaker) -> None:
    # The WITH CHECK on the external-domain policy rejects a scoped INSERT outright.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text("INSERT INTO app.research_run_state (run_id, question) VALUES (:r, 'q')"),
                {"r": _run_id()},
            )


async def test_scoped_principal_cannot_claim(maker: async_sessionmaker) -> None:
    rid = _run_id()
    await _create(maker, OWNER_EXT, rid)
    assert await rrs.claim_resume(maker, GENERAL_ONLY, rid) is False  # can't see → can't claim
    assert await rrs.claim_resume(maker, OWNER_EXT, rid) is True  # the owner's claim still wins
