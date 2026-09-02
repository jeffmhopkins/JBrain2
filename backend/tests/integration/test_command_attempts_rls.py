"""Migration 0182 against real Postgres: `app.command_attempts` is owner-only.

Two different things need protecting here, and they pull in opposite directions.

**Reading** the log tells you the owner's command WORDS and which stations are allowed
to use them — the exact reconnaissance an attacker wants before transmitting a forgery.

**Writing** it is worse: this table is the record of what was tried. A non-owner able to
insert could bury a real attempt under noise, and one able to delete could erase the
evidence entirely. So the test that matters most is not "the read returns nothing" but
"the row is still there afterwards" (docs/plans/APRS_CONTROL_PLAN.md P4).
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
EVERY_SCOPE = SessionContext(
    principal_kind="capability_token",
    domain_scopes=("general", "health", "finance", "location"),
)

_INSERT = text(
    "INSERT INTO app.command_attempts (source, word, code, accepted, reason)"
    " VALUES (:src, :word, :code, :ok, :reason)"
)
_ROW = {
    "src": "KE8XYZ-9",
    "word": "GATE",
    "code": "7K2M9",
    "ok": False,
    "reason": "code did not verify",
}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def one_attempt(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(_INSERT, _ROW)
        await s.commit()
    yield
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.command_attempts"))
        await s.commit()


async def test_the_owner_reads_what_was_tried(maker: async_sessionmaker, one_attempt: None) -> None:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(text("SELECT source, word, accepted FROM app.command_attempts"))
        ).all()

    assert [(r.source, r.word, r.accepted) for r in rows] == [("KE8XYZ-9", "GATE", False)]


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_sees_nothing(
    maker: async_sessionmaker, one_attempt: None, ctx_name: str
) -> None:
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(text("SELECT id FROM app.command_attempts"))).all()

    # Holding every domain scope is still not being the owner: the command words this
    # would leak are reconnaissance, not domain-scoped data.
    assert rows == []


@pytest.mark.parametrize("ctx_name", ["GENERAL_ONLY", "EVERY_SCOPE", "UNSCOPED"])
async def test_a_non_owner_cannot_forge_an_attempt(
    maker: async_sessionmaker, ctx_name: str
) -> None:
    ctx = {"GENERAL_ONLY": GENERAL_ONLY, "EVERY_SCOPE": EVERY_SCOPE, "UNSCOPED": UNSCOPED}[ctx_name]

    with pytest.raises((ProgrammingError, DBAPIError)):
        async with scoped_session(maker, ctx) as s:
            await s.execute(_INSERT, _ROW)
            await s.commit()


async def test_a_non_owner_cannot_erase_the_evidence(
    maker: async_sessionmaker, one_attempt: None
) -> None:
    async with scoped_session(maker, GENERAL_ONLY) as s:
        await s.execute(text("DELETE FROM app.command_attempts"))
        await s.commit()

    # RLS makes the DELETE match no rows rather than error, so what proves the policy is
    # that the attempt SURVIVED — covering tracks is the thing being stopped.
    async with scoped_session(maker, OWNER) as s:
        rows = (await s.execute(text("SELECT id FROM app.command_attempts"))).all()
    assert len(rows) == 1


async def test_deleting_a_task_keeps_the_attempts_made_against_it(
    maker: async_sessionmaker,
) -> None:
    # An attempt outlives the command it was aimed at: the history of what was tried is
    # exactly what the owner would go looking for after deleting a command word.
    async with scoped_session(maker, OWNER) as s:
        principal = OWNER.principal_id
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (:pid, 'owner', :hash) ON CONFLICT (id) DO NOTHING"
            ),
            {"pid": principal, "hash": f"h-{principal}"},
        )
        task_id = (
            await s.execute(
                text(
                    "INSERT INTO app.tasks (principal_id, name, prompt, agent, schedule_kind,"
                    " command_word, command_key)"
                    " VALUES (:pid, 'Gate', 'open it', 'jerv', 'on_command', 'GATE', :key)"
                    " RETURNING id"
                ),
                # A real-length key: the CHECK requires one, because an empty or short
                # key is not a broken credential but a publicly computable one.
                {"pid": principal, "key": "A" * 52},
            )
        ).scalar()
        await s.execute(
            text(
                "INSERT INTO app.command_attempts (task_id, source, word, code, accepted, reason)"
                " VALUES (:tid, 'KE8XYZ-9', 'GATE', '7K2M9', true, 'verified')"
            ),
            {"tid": task_id},
        )
        await s.commit()

    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.tasks WHERE id = :id"), {"id": task_id})
        await s.commit()

    async with scoped_session(maker, OWNER) as s:
        rows = (
            (await s.execute(text("SELECT task_id, word FROM app.command_attempts")))
            .mappings()
            .all()
        )
        await s.execute(text("DELETE FROM app.command_attempts"))
        await s.commit()

    assert [(r["task_id"], r["word"]) for r in rows] == [(None, "GATE")]
