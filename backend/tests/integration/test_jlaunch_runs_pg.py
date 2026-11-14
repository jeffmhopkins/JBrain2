"""Migration 0269 against real Postgres: each job's runs are numbered v1, v2, … for good,
a lost run is failed, and the owner's note lands — all on the owner-only RLS mirror."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import jlaunch_share_context, scoped_session
from jbrain.models.jlaunch import JlaunchRunRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_REPO = JlaunchRunRepo()


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _job(jid: str, spec: str, state: str = "succeeded") -> dict[str, object]:
    return {"id": jid, "spec_name": spec, "title": spec, "state": state, "phase": "train"}


async def test_runs_are_numbered_per_job_and_keep_their_number(
    maker: async_sessionmaker,
) -> None:
    # Spec names of this test's own, so a shared database's other runs don't number in.
    walk, look = f"walk-{uuid.uuid4().hex[:8]}", f"look-{uuid.uuid4().hex[:8]}"
    async with scoped_session(maker, OWNER) as db:
        for jid, spec in (("rv1", walk), ("rv2", walk), ("rl1", look), ("rv3", walk)):
            await _REPO.upsert(db, _job(jid, spec))
        # A re-upsert (every poll) must not renumber the run.
        await _REPO.upsert(db, _job("rv1", walk))
        await _REPO.delete(db, "rv2")
        await _REPO.upsert(db, _job("rv4", walk))
        mine = {"rv1", "rv3", "rv4", "rl1"}
        revs = {r.id: r.revision for r in await _REPO.list(db) if r.id in mine}
    assert revs == {"rv1": 1, "rv3": 3, "rv4": 4, "rl1": 1}


async def test_lost_run_fails_and_note_sticks(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, OWNER) as db:
        await _REPO.upsert(db, _job("ghost1", "walk", state="running"))
        await _REPO.mark_lost(db, "ghost1")
        assert await _REPO.set_note(db, "ghost1", "lower lr")
        assert not await _REPO.set_note(db, "nosuch", "x")
        row = await _REPO.get(db, "ghost1")
    assert row is not None
    assert (row.state, row.note) == ("failed", "lower lr")
    assert row.error.startswith("lost")


async def test_a_share_visitor_cannot_note_a_run(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, OWNER) as db:
        await _REPO.upsert(db, _job("shr1", "walk"))
    # No link pins this run, so RLS hides it from the visitor: the update touches nothing.
    async with scoped_session(
        maker, jlaunch_share_context("00000000-0000-0000-0000-000000000000")
    ) as db:
        assert not await _REPO.set_note(db, "shr1", "vandal")
    async with scoped_session(maker, OWNER) as db:
        row = await _REPO.get(db, "shr1")
    assert row is not None and row.note == ""
