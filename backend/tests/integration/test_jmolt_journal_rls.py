"""Migration 0176 against real Postgres: the jmolt journal's M19 firewall (CLAUDE.md
rule 3, JMOLT_PLAN M19).

The journal is jmolt's append-only line to its human, and it wears the same M19 split as
the scratchpad (0173): jmolt and jerv both run as the owner principal, so is_owner() can't
separate them. SELECT is gated on the jmolt domain scope; INSERT on auth_context='jmolt'
AND the session's own principal id; there is NO UPDATE at all (entries are immutable);
DELETE (the retention prune) needs jmolt's auth context. This proves (a) jmolt appends and
reads its own entries; (b) jerv's observation session reads but cannot append; (c) a
session in neither domain sees nothing and cannot append; (d) an entry can never be
UPDATEd by anyone; (e) jmolt cannot append a row for another principal; (f) the per-entry
byte cap and the retention bound hold.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmoltjournaltools import build_jmolt_journal_handlers
from jbrain.agent.loop import ToolContext
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt import JOURNAL_RETENTION, MAX_JOURNAL_BYTES, JmoltJournalRepo
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


# The shared testcontainer DB persists rows across tests; clear the journal (jmolt may
# DELETE it via the prune) before each so count/order assertions are per-test.
_JMOLT_CLEAN = SessionContext(
    principal_kind="owner", domain_scopes=("jmolt",), auth_context="jmolt", owner_scoped=True
)


@pytest.fixture(autouse=True)
async def _clean_journal(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _JMOLT_CLEAN) as s:
        await s.execute(text("DELETE FROM app.jmolt_journal"))
    yield


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


def _jmolt_ctx(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _jerv_observe_ctx(pid: str) -> SessionContext:
    # jerv's observation session: owner + jmolt read scope, but NO jmolt auth context.
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        owner_scoped=True,
    )


# A non-owner in neither domain.
_OUTSIDER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


async def test_jmolt_appends_and_reads_its_own_entries(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltJournalRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.add(s, pid, "quiet night, mostly read")
        await repo.add(s, pid, "the tide-pool submol keeps pulling me back")
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        entries = await repo.recent(s, pid)
    # Newest first.
    assert [e.content for e in entries] == [
        "the tide-pool submol keeps pulling me back",
        "quiet night, mostly read",
    ]


async def test_a_blank_entry_is_a_noop(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltJournalRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.add(s, pid, "   \n  ")
        assert await repo.recent(s, pid) == []


async def test_jerv_can_read_but_never_append(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltJournalRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.add(s, pid, "a real entry")

    # jerv's observation session READS fine (jmolt domain scope grants SELECT).
    async with scoped_session(maker, _jerv_observe_ctx(pid)) as s:
        assert [e.content for e in await repo.recent(s, pid)] == ["a real entry"]

    # …but an append is denied by RLS (no jmolt auth context).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _jerv_observe_ctx(pid)) as s:
            await repo.add(s, pid, "jerv forging an entry")

    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        assert len(await repo.recent(s, pid)) == 1  # unchanged


async def test_no_one_can_update_an_entry(maker: async_sessionmaker) -> None:
    # There is no UPDATE grant or policy at all — an entry is immutable once written.
    pid = await _owner_pid(maker)
    repo = JmoltJournalRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.add(s, pid, "original words")
    # Even jmolt's own auth context cannot UPDATE (no grant).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _jmolt_ctx(pid)) as s:
            await s.execute(
                text("UPDATE app.jmolt_journal SET content = 'x' WHERE principal_id = :pid"),
                {"pid": pid},
            )


async def test_outsider_sees_nothing_and_cannot_append(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltJournalRepo()
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.add(s, pid, "private to jmolt and its human")

    async with scoped_session(maker, _OUTSIDER) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.jmolt_journal"))).scalar()
    assert count == 0  # RLS hides every row from a session without the jmolt scope.

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _OUTSIDER) as s:
            await s.execute(
                text("INSERT INTO app.jmolt_journal (principal_id, content) VALUES (:pid, 'x')"),
                {"pid": pid},
            )


async def test_jmolt_cannot_append_for_another_principal(maker: async_sessionmaker) -> None:
    # M19(d) — jmolt writes only its OWN rows; the WITH CHECK pins principal_id.
    pid = await _owner_pid(maker)
    repo = JmoltJournalRepo()
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _jmolt_ctx(pid)) as s:
            await repo.add(s, "some-other-principal", "not mine")


async def test_entry_is_truncated_and_history_is_retention_bounded(
    maker: async_sessionmaker,
) -> None:
    pid = await _owner_pid(maker)
    repo = JmoltJournalRepo()
    # Per-entry cap: an oversize entry is truncated, never rejected.
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await repo.add(s, pid, "y" * (MAX_JOURNAL_BYTES + 100))
        stored = (await repo.recent(s, pid))[0].content
    assert len(stored.encode("utf-8")) <= MAX_JOURNAL_BYTES

    # Retention: past the cap, the oldest entries are pruned to the bound.
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        await s.execute(text("DELETE FROM app.jmolt_journal"))
        for i in range(JOURNAL_RETENTION + 10):
            await repo.add(s, pid, f"entry {i}")
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        entries = await repo.recent(s, pid, limit=JOURNAL_RETENTION + 50)
    assert len(entries) == JOURNAL_RETENTION  # bounded
    assert entries[0].content == f"entry {JOURNAL_RETENTION + 9}"  # newest kept


async def test_journal_handler_roundtrips_under_jmolt_context(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    handlers = build_jmolt_journal_handlers(maker)
    ctx = ToolContext(session=_jmolt_ctx(pid), scopes=())

    assert "needs an `entry`" in await handlers["journal"]({}, ctx)
    assert "Left a note" in await handlers["journal"]({"entry": "goodnight"}, ctx)
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        assert [e.content for e in await JmoltJournalRepo().recent(s, pid)] == ["goodnight"]
