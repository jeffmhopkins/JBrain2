"""Migration 0181 against real Postgres: the simulator corpus is owner-only, never jmolt.

A corpus is third-party text harvested from a hostile-by-assumption platform, PLUS a record of
jmolt's own past behaviour. jmolt reading one back would be exactly the re-entry the threat
model forbids — its own transcript returning as trusted context, carrying whatever the
platform said inside it. So the split here is the settings-table one (`is_owner()` AND
`auth_ctx() <> 'jmolt'`), not the jmolt-domain-scope one its own tables use.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_sim import SimCorpusRepo
from jbrain.agent.jmolt_sim_client import SimCorpus
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
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


def _owner(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner")


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _outsider() -> SessionContext:
    return SessionContext(principal_id="someone", principal_kind="capability_token")


def _corpus() -> SimCorpus:
    return SimCorpus(handle="jmolt", posts={"p1": {"id": "p1", "title": "t"}})


async def _save(maker, ctx: SessionContext, note: str = "a night") -> str:
    async with scoped_session(maker, ctx) as s:
        return await SimCorpusRepo().save(
            s, corpus=_corpus(), scratch={"open.md": "a question"}, note=note
        )


async def test_the_owner_can_save_list_and_read_a_corpus(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    cid = await _save(maker, _owner(pid))
    async with scoped_session(maker, _owner(pid)) as s:
        stored = await SimCorpusRepo().get(s, cid)
        listed = await SimCorpusRepo().list(s)
    assert stored is not None
    assert stored.corpus.posts["p1"]["title"] == "t"
    assert stored.scratch == {"open.md": "a question"}
    assert cid in {c.id for c in listed}


async def test_jmolt_cannot_read_a_corpus(maker: async_sessionmaker) -> None:
    """The property this table exists to have. A corpus holds jmolt's own past behaviour and
    the platform's text; handing it back to jmolt is the re-entry the threat model forbids."""
    pid = await _owner_pid(maker)
    cid = await _save(maker, _owner(pid))
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await SimCorpusRepo().get(s, cid) is None
        assert await SimCorpusRepo().list(s) == []


async def test_jmolt_cannot_write_or_delete_a_corpus(maker: async_sessionmaker) -> None:
    """Not only reading: jmolt planting a corpus would be jmolt choosing what a future
    measurement of jmolt is taken against."""
    pid = await _owner_pid(maker)
    cid = await _save(maker, _owner(pid))
    async with scoped_session(maker, _jmolt(pid)) as s:
        with pytest.raises(Exception):  # noqa: B017 — RLS refuses; the class is the driver's
            await SimCorpusRepo().save(s, corpus=_corpus(), scratch={}, note="planted")
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert await SimCorpusRepo().delete(s, cid) is False  # the row is invisible to it
    async with scoped_session(maker, _owner(pid)) as s:
        assert await SimCorpusRepo().get(s, cid) is not None  # and still there


async def test_an_outsider_sees_nothing(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    await _save(maker, _owner(pid))
    async with scoped_session(maker, _outsider()) as s:
        assert await SimCorpusRepo().list(s) == []


async def test_the_owner_can_delete_a_corpus(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    cid = await _save(maker, _owner(pid))
    async with scoped_session(maker, _owner(pid)) as s:
        assert await SimCorpusRepo().delete(s, cid) is True
        assert await SimCorpusRepo().get(s, cid) is None
