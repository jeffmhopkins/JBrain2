"""The `skill_sweep` action (Loop 2, Wave 3) against real Postgres: per-domain usefulness-decay
eviction demotes the least-useful ACTIVE skills to shadow (reversible), keeps the cap most-useful,
leaves quarantined/shadow skills untouched, converges on re-run, refuses behind the kill-switch, and
is domain-firewalled by RLS. Embeddings are irrelevant here (the ranking is on success_stats)."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.agent.skills import SkillsRepo
from jbrain.agent.skillsweep import SkillSweepAction
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from jbrain.settings_store import (
    SELF_IMPROVEMENT_KILL_SWITCH_KEY,
    SqlSettingsStore,
)
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


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


async def _seed_skill(
    maker: async_sessionmaker,
    *,
    name: str,
    domain: str,
    surfaced: int | None,
    age_days: int,
    status: str = "active",
) -> str:
    """An ACTIVE-by-default skill with a controlled usefulness signal: `surfaced` count (None =
    never surfaced) and a `last_surfaced_at`/`created_at` `age_days` ago (older = staler)."""
    stats = "'{}'::jsonb"
    if surfaced is not None:
        stats = (
            "jsonb_build_object('surfaced', :surfaced::int,"
            " 'last_surfaced_at', (now() - make_interval(days => :age)))"
        )
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.skills"
                        " (id, name, version, status, domain_code, body, description,"
                        "  success_stats, created_at)"
                        " VALUES (gen_random_uuid(), :name, 1, :status, :domain, 'b', 'd',"
                        f"  {stats}, now() - make_interval(days => :age))"
                        " RETURNING id::text"
                    ),
                    {
                        "name": name,
                        "status": status,
                        "domain": domain,
                        "surfaced": surfaced or 0,
                        "age": age_days,
                    },
                )
            ).scalar_one()
        )


async def _status(maker: async_sessionmaker, skill_id: str) -> str:
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text("SELECT status FROM app.skills WHERE id = :i"), {"i": skill_id}
                )
            ).scalar_one()
        )


def _action(maker: async_sessionmaker) -> SkillSweepAction:
    return SkillSweepAction(maker, settings=SqlSettingsStore(maker), skills=SkillsRepo(maker))


async def test_sweep_keeps_the_cap_most_useful_and_demotes_the_rest(
    maker: async_sessionmaker,
) -> None:
    await _owner_pid(maker)
    # Three active general skills; cap=2 → the least-useful is demoted. `fresh` was surfaced
    # recently, `busy` a lot but a while ago, `stale` rarely and long ago.
    fresh = await _seed_skill(maker, name="fresh", domain="general", surfaced=2, age_days=1)
    busy = await _seed_skill(maker, name="busy", domain="general", surfaced=50, age_days=10)
    stale = await _seed_skill(maker, name="stale", domain="general", surfaced=1, age_days=90)
    await SqlSettingsStore(maker).upsert(OWNER, "skill_active_cap_per_domain", 2)

    await _action(maker).run({})

    # Recency leads the decay order, so the long-stale skill is the one evicted; the rest stay.
    assert await _status(maker, stale) == "shadow"
    assert await _status(maker, fresh) == "active"
    assert await _status(maker, busy) == "active"


async def test_sweep_is_per_domain_and_ignores_quarantined_and_shadow(
    maker: async_sessionmaker,
) -> None:
    await _owner_pid(maker)
    # cap=1 per domain. A quarantined general skill and a shadow general skill are NOT in the active
    # set, so they neither count toward the cap nor get touched; one active general survives.
    keep = await _seed_skill(maker, name="keep", domain="general", surfaced=10, age_days=1)
    drop = await _seed_skill(maker, name="drop", domain="general", surfaced=1, age_days=30)
    quar = await _seed_skill(
        maker, name="quar", domain="general", surfaced=99, age_days=1, status="quarantined"
    )
    shad = await _seed_skill(
        maker, name="shad", domain="general", surfaced=99, age_days=1, status="shadow"
    )
    # A health skill is in another domain's partition — the general cap never evicts it.
    health = await _seed_skill(maker, name="h", domain="health", surfaced=0, age_days=99)
    await SqlSettingsStore(maker).upsert(OWNER, "skill_active_cap_per_domain", 1)

    await _action(maker).run({})

    assert await _status(maker, keep) == "active"  # the one most-useful general active survives
    assert await _status(maker, drop) == "shadow"  # the surplus active is demoted
    assert await _status(maker, quar) == "quarantined"  # untouched, not counted
    assert await _status(maker, shad) == "shadow"  # already shadow, untouched
    assert await _status(maker, health) == "active"  # other domain, separate cap


async def test_sweep_is_idempotent(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    a = await _seed_skill(maker, name="a", domain="general", surfaced=5, age_days=1)
    b = await _seed_skill(maker, name="b", domain="general", surfaced=1, age_days=30)
    await SqlSettingsStore(maker).upsert(OWNER, "skill_active_cap_per_domain", 1)

    demoted_first = await SkillsRepo(maker).demote_over_cap(OWNER, 1)
    demoted_again = await SkillsRepo(maker).demote_over_cap(OWNER, 1)
    assert {d[0] for d in demoted_first} == {b}  # the surplus is demoted once
    assert demoted_again == []  # already at the cap — a re-run demotes nothing
    assert await _status(maker, a) == "active"


async def test_sweep_refused_when_kill_switch_on(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    over = await _seed_skill(maker, name="x", domain="general", surfaced=0, age_days=1)
    extra = await _seed_skill(maker, name="y", domain="general", surfaced=0, age_days=2)
    await SqlSettingsStore(maker).upsert(OWNER, SELF_IMPROVEMENT_KILL_SWITCH_KEY, True)
    await SqlSettingsStore(maker).upsert(OWNER, "skill_active_cap_per_domain", 1)
    from jbrain.queue import PermanentJobError

    with pytest.raises(PermanentJobError):
        await _action(maker).run({})
    # Nothing demoted behind the gate — both stay active.
    assert await _status(maker, over) == "active" and await _status(maker, extra) == "active"


async def test_demote_is_domain_firewalled(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    gen = await _seed_skill(maker, name="g", domain="general", surfaced=0, age_days=1)
    health = await _seed_skill(maker, name="h", domain="health", surfaced=0, age_days=1)
    # A general-narrowed session running the eviction (cap=0 → demote everything visible) can only
    # see + demote general skills; the health skill is invisible under RLS, so it is never touched.
    await SkillsRepo(maker).demote_over_cap(read_context(pid, ("general",)), 0)
    assert await _status(maker, gen) == "shadow"
    assert await _status(maker, health) == "active"  # firewalled — out of the narrowed scope
