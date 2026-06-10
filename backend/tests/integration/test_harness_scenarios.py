"""Golden LLM-in-the-middle scenarios, run against real Postgres.

Each file under tests/harness/scenarios/ scripts the note.extract output a
perfect model would return and asserts the resulting graph. A scenario with an
`xfail` reason encodes behaviour a known-open bug doesn't satisfy yet; it's
marked xfail(strict) so the fix flips it green and fails loudly if it lingers.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.conftest import docker_available
from tests.harness.runner import run_scenario
from tests.harness.scenario import Scenario, check, load_all
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _params() -> list:
    params = []
    for sc in load_all():
        marks = [pytest.mark.xfail(reason=sc.xfail, strict=True)] if sc.xfail else []
        params.append(pytest.param(sc, id=sc.source, marks=marks))
    return params


SCENARIOS = _params()


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Each scenario starts from an empty graph. Truncate at SETUP, not
    teardown: a test must not inherit rows if a previous test's teardown was
    skipped (e.g. a dropped connection), so each scenario guarantees its own
    clean slate regardless of what ran before."""
    from sqlalchemy import text

    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        await s.execute(
            text(
                "TRUNCATE app.facts, app.entities, app.entity_mentions, app.entity_aliases,"
                " app.temporal_tokens, app.review_items, app.note_analysis,"
                " app.chunks, app.notes CASCADE"
            )
        )
        await s.commit()
    yield


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_scenario(scenario: Scenario, maker: async_sessionmaker[AsyncSession]) -> None:
    snapshot = await run_scenario(maker, scenario)
    failures = check(snapshot, scenario.expect)
    assert not failures, "\n".join(failures)
