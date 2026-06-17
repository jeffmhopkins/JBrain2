"""Skill repo/retrieval (Loop 2, Wave 1) against real Postgres: the active-only invariant (the RLS
policy gates on domain, NOT status, so 'shadow never surfaced' must be query-enforced) and the
domain firewall (a session sees only skills in a domain it holds), plus the surfaced counter."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.agent.skills import SkillsRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

VEC = [0.0] * 384


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        return str(
            (await s.execute(text("SELECT id FROM app.principals WHERE kind='owner'"))).scalar()
        )


async def _seed(repo: SkillsRepo) -> dict[str, str]:
    gen = await repo.create(
        OWNER,
        name=f"g-{uuid.uuid4().hex[:6]}",
        description="cite a general fact",
        body="1. search 2. read_note",
        domain_code="general",
        status="active",
        embedding=VEC,
    )
    health = await repo.create(
        OWNER,
        name=f"h-{uuid.uuid4().hex[:6]}",
        description="check a medication",
        body="1. read_entity",
        domain_code="health",
        status="active",
        embedding=VEC,
    )
    shadow = await repo.create(
        OWNER,
        name=f"s-{uuid.uuid4().hex[:6]}",
        description="draft general shadow",
        body="1. search",
        domain_code="general",
        status="shadow",
        embedding=VEC,
    )
    return {"gen": gen, "health": health, "shadow": shadow}


async def test_recall_is_active_only(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    repo = SkillsRepo(maker)
    ids = await _seed(repo)
    hits = await repo.recall_dense(OWNER, VEC, 10)
    got = {h.id for h in hits}
    assert ids["gen"] in got and ids["health"] in got
    assert ids["shadow"] not in got  # shadow is excluded by the query, NOT by RLS


async def test_recall_is_domain_firewalled(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = SkillsRepo(maker)
    ids = await _seed(repo)
    # A narrowed owner (general only) sees the general active skill, not the health one.
    narrowed = await repo.recall_dense(read_context(pid, ("general",)), VEC, 10)
    got = {h.id for h in narrowed}
    assert ids["gen"] in got and ids["health"] not in got
    # A capability token scoped to a domain that holds neither sees nothing.
    finance = SessionContext(principal_kind="capability_token", domain_scopes=("finance",))
    assert await repo.recall_dense(finance, VEC, 10) == []


async def test_record_surfaced_bumps_the_counter(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    repo = SkillsRepo(maker)
    ids = await _seed(repo)
    # One call bumps each matched row once (a turn passes a deduped id set); two turns → 2.
    await repo.record_surfaced(OWNER, [ids["gen"]])
    await repo.record_surfaced(OWNER, [ids["gen"]])
    async with scoped_session(maker, OWNER) as s:
        surfaced = (
            await s.execute(
                text("SELECT (success_stats->>'surfaced')::int FROM app.skills WHERE id = :i"),
                {"i": ids["gen"]},
            )
        ).scalar()
    assert surfaced == 2
