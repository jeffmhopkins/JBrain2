"""Promise extraction into the obligation ledger (JMOLT_LEDGER_ENGINE_PLAN.md, S2).

The two halves are tested apart (patterns in test_jmolt_promise, the ledger in
test_jmolt_obligation_rls); this is the joint they make. What it has to demonstrate is the
actual claim: an agent that said "I'll come back to this" is asked about it by tomorrow's
context, whether or not it remembered saying so.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_compose import compose_brief
from jbrain.agent.jmolt_promise import find_promises
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_obligation import ObligationRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

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
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _admin(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_obligation"))
    yield


async def _record(maker, pid: str, published: str) -> None:
    """What the engine does after a write goes out: read the text back for promises and open
    one obligation per promise, with the sentence as evidence."""
    repo = ObligationRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        for promise in find_promises(published):
            await repo.open(
                s,
                pid,
                kind="commitment",
                subject=promise.subject,
                quote=promise.quote,
                source="self",
            )


async def test_a_promise_made_tonight_is_in_front_of_it_tomorrow(
    maker: async_sessionmaker,
) -> None:
    """The whole claim, end to end. Nothing here asked the model whether it promised anything
    — which is the point, since a model shown its own text produces whatever continues it."""
    pid = await _owner_pid(maker)
    await _record(
        maker,
        pid,
        "Weeks are a strange unit for an agent. I'll come back to this tomorrow with numbers.",
    )
    async with scoped_session(maker, _jmolt(pid)) as s:
        obligations = await ObligationRepo().open_(s, pid)
    brief = compose_brief(
        handle="jmolt",
        now=datetime(2026, 8, 31, 3, 5, tzinfo=UTC),
        minutes_left=55,
        open_obligations=obligations,
        closed_recently=[],
    )
    assert "I'll come back to this tomorrow with numbers." in brief
    assert "you said:" in brief  # attributed as its own, on a commitment, which is allowed


async def test_repeating_the_promise_the_next_night_does_not_double_it(
    maker: async_sessionmaker,
) -> None:
    """An agent that restates a promise has still made one promise. Two rows would print as
    two lines, which is the repetition the brief is supposed to stop reflecting back."""
    pid = await _owner_pid(maker)
    for _ in range(3):
        await _record(maker, pid, "I'll come back to this tomorrow.")
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await ObligationRepo().counts(s, pid) == {"commitment": 1}


async def test_discharging_it_takes_it_out_of_the_brief(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _record(maker, pid, "I'll count them next time.")
    async with scoped_session(maker, _jmolt(pid)) as s:
        repo = ObligationRepo()
        [ob] = await repo.open_(s, pid)
        assert await repo.close(s, pid, ob.id, resolution="counted: 14 of them") is True
        assert await repo.open_(s, pid) == []
        [done] = await repo.closed_since(s, pid, since=ob.opened_at)
    brief = compose_brief(
        handle="jmolt",
        now=datetime(2026, 8, 31, 3, 5, tzinfo=UTC),
        minutes_left=55,
        open_obligations=[],
        closed_recently=[done],
    )
    assert "counted: 14 of them" in brief
    assert "nothing open" in brief.lower()


async def test_a_post_with_no_promise_leaves_the_ledger_alone(maker: async_sessionmaker) -> None:
    """Most posts are not promises, and an engine that opened an obligation per post would
    hand tomorrow a brief made entirely of its own noise."""
    pid = await _owner_pid(maker)
    await _record(maker, pid, "Weeks are a strange unit. You should look into it.")
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await ObligationRepo().counts(s, pid) == {}
