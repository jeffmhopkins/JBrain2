"""EvalRunStore against real Postgres (docs/WORKFLOW_ENGINE_PLAN.md §5 Track C):
the eval-run round-trip preserves the task/safety split, `latest` returns the most
recent run for a (suite, version_label), and the owner-only RLS on app.eval_runs
holds — a narrowed-but-owner session still reads it (audit metadata, the agent_runs
precedent), a non-owner token session cannot."""

import uuid
from collections.abc import AsyncIterator

import pytest
from evals.promotion import EvalRun, FixtureScore
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext
from jbrain.workflow.evalstore import EvalRunStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A narrowed owner session (migration 0015): still an owner, so owner-only audit
# rows stay visible — the agent_runs / eval_runs posture.
OWNER_HEALTH = SessionContext(
    principal_id=str(uuid.uuid4()),
    principal_kind="owner",
    domain_scopes=("health",),
    owner_scoped=True,
)
HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _run(version: str, *scores: tuple[str, float, float]) -> EvalRun:
    return EvalRun(version, tuple(FixtureScore(f, t, s) for f, t, s in scores))


async def test_round_trip_preserves_task_and_safety(maker: async_sessionmaker) -> None:
    """A saved run reconstructs with both dimensions intact — a flat blob would
    defeat the gate (the whole point of FixtureScore)."""
    store = EvalRunStore(maker)
    suite = f"suite-{uuid.uuid4().hex[:8]}"
    run = _run("v1", ("alpha", 0.75, 1.0), ("beta", 1.0, 0.5))
    await store.save(OWNER, run, suite=suite, model="fake-model", new_case="beta")

    loaded = await store.latest(OWNER, suite=suite, version_label="v1")
    assert loaded is not None
    by = loaded.by_fixture()
    assert by["alpha"] == FixtureScore("alpha", 0.75, 1.0)
    assert by["beta"] == FixtureScore("beta", 1.0, 0.5)


async def test_latest_returns_the_most_recent_run(maker: async_sessionmaker) -> None:
    store = EvalRunStore(maker)
    suite = f"suite-{uuid.uuid4().hex[:8]}"
    await store.save(OWNER, _run("v1", ("a", 0.2, 1.0)), suite=suite, model="m")
    await store.save(OWNER, _run("v1", ("a", 0.9, 1.0)), suite=suite, model="m")
    loaded = await store.latest(OWNER, suite=suite, version_label="v1")
    assert loaded is not None
    assert loaded.by_fixture()["a"].task == 0.9  # the second (latest) write wins


async def test_latest_is_none_for_an_unscored_label(maker: async_sessionmaker) -> None:
    store = EvalRunStore(maker)
    assert await store.latest(OWNER, suite="nope", version_label="never") is None


async def test_eval_runs_are_owner_only(maker: async_sessionmaker) -> None:
    """The store's writes/reads obey the owner-only RLS on app.eval_runs: a narrowed
    owner still reads its own run; a non-owner token session sees nothing (rule 3)."""
    store = EvalRunStore(maker)
    suite = f"suite-{uuid.uuid4().hex[:8]}"
    await store.save(OWNER, _run("v1", ("a", 1.0, 1.0)), suite=suite, model="m")

    # A domain-narrowed owner is still an owner — audit metadata stays visible.
    assert await store.latest(OWNER_HEALTH, suite=suite, version_label="v1") is not None
    # A non-owner scoped token cannot see (or write) the owner's eval runs.
    assert await store.latest(HEALTH_ONLY, suite=suite, version_label="v1") is None
    assert await store.latest(UNSCOPED, suite=suite, version_label="v1") is None


async def test_non_owner_cannot_write_an_eval_run(maker: async_sessionmaker) -> None:
    """A scoped non-owner write is refused by the owner-only WITH CHECK — the row
    never lands (the owner reads zero of them back)."""
    from sqlalchemy.exc import ProgrammingError

    store = EvalRunStore(maker)
    suite = f"suite-{uuid.uuid4().hex[:8]}"
    with pytest.raises(ProgrammingError):
        await store.save(HEALTH_ONLY, _run("v1", ("a", 1.0, 1.0)), suite=suite, model="m")
    # Nothing landed under the owner either.
    assert await store.latest(OWNER, suite=suite, version_label="v1") is None
