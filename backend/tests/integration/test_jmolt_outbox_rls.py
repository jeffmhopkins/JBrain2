"""Migration 0174 against real Postgres: the outbox/ledger authority split (M7/M14/M19).

The load-bearing property: jmolt STAGES writes but can never RELEASE them (only a non-jmolt
owner — the PWA or the system sweep — can), so jmolt cannot self-publish past the review
queue. Plus: an outsider sees nothing; jmolt writes only its own rows; the ledger is
append-only to jmolt (only the system prunes).
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
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


def _owner_admin(pid: str) -> SessionContext:
    # The PWA owner / system sweep: owner, jmolt read scope, NO jmolt auth context.
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


_OUTSIDER = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    # A non-jmolt owner can purge both tables (system authority).
    async with scoped_session(
        maker, SessionContext(principal_kind="owner", domain_scopes=("jmolt",))
    ) as s:
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
        await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
    yield


async def test_jmolt_stages_but_cannot_release(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    outbox = OutboxRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        row_id = await outbox.stage(s, pid, kind="post", payload={"title": "hi", "content": "x"})
    assert row_id is not None  # no dedup_key on a post, so a fresh stage always returns an id

    # jmolt cannot advance its own row to 'released' — the UPDATE policy hides it (0 rows).
    async with scoped_session(maker, _jmolt(pid)) as s:
        res = await s.execute(
            text("UPDATE app.jmolt_outbox SET status = 'released' WHERE id = :id"), {"id": row_id}
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]
    async with scoped_session(maker, _jmolt(pid)) as s:
        rows = await outbox.list_by_status(s, pid, ("queued",))
    assert len(rows) == 1 and rows[0].status == "queued"  # still queued

    # A non-jmolt owner (PWA/sweep) CAN release it.
    async with scoped_session(maker, _owner_admin(pid)) as s:
        await outbox.set_status(s, row_id, "released")
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert (await outbox.list_by_status(s, pid, ("released",)))[0].status == "released"


async def test_outsider_sees_no_outbox_and_cannot_stage(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    outbox = OutboxRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        await outbox.stage(s, pid, kind="comment", payload={"content": "hello"})
    async with scoped_session(maker, _OUTSIDER) as s:
        count = (await s.execute(text("SELECT count(*) FROM app.jmolt_outbox"))).scalar()
    assert count == 0
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _OUTSIDER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.jmolt_outbox (principal_id, kind, payload)"
                    " VALUES (:pid, 'post', '{}'::jsonb)"
                ),
                {"pid": pid},
            )


async def test_jmolt_cannot_stage_for_another_principal(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    outbox = OutboxRepo()
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, _jmolt(pid)) as s:
            await outbox.stage(s, "someone-else", kind="post", payload={"title": "t"})


async def test_ledger_append_and_prune_authority(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    ledger = ActionLedgerRepo()
    async with scoped_session(maker, _jmolt(pid)) as s:
        await ledger.record(
            s, pid, action="web_fetch", target="https://example.com", reacted_to="a post linked it"
        )
        await ledger.record(s, pid, action="comment", target="post123")
    async with scoped_session(maker, _jmolt(pid)) as s:
        recent = await ledger.recent(s, pid)
    assert [r.action for r in recent] == ["comment", "web_fetch"]  # newest first

    # jmolt cannot delete the ledger (DELETE policy hides rows) — it is append-only to jmolt.
    async with scoped_session(maker, _jmolt(pid)) as s:
        res = await s.execute(
            text("DELETE FROM app.jmolt_action_ledger WHERE principal_id = :pid"), {"pid": pid}
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]
    # The system (non-jmolt owner) can prune.
    async with scoped_session(maker, _owner_admin(pid)) as s:
        await ledger.prune(s, pid, keep=1)
    async with scoped_session(maker, _jmolt(pid)) as s:
        assert len(await ledger.recent(s, pid)) == 1
