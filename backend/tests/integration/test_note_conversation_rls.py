"""Migration 0191 against real Postgres: RLS isolation for `app.note_conversations` and
`app.note_conversation_tool_calls` (CLAUDE.md rule 3 — every new table gets one).

Owner-only, ENABLE + FORCE, like `agent_session_plans`. Three properties matter:

- a capability token sees nothing on either table and cannot write one;
- a NARROWED owner (`owner_scoped=True`) still sees its own threads, because the notes
  inbox lists "threads waiting on you" (D4/D5) and narrowing that list would hide a
  question the owner can never otherwise learn exists. What narrowing DOES hide is the
  note itself, which keeps its own domain policy — the deliberate split 0191 defends;
- an unscoped session is refused outright.

Plus the two grant decisions: a conversation row cannot be DELETEd (it dies with its
session, so erasing it alone would strand a transcript full of the note's body), and a
ledger row takes UPDATE on `turn_id` and nothing else.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A capability token holding one domain: `app.is_owner()` is false, so it is outside
# both policies however wide its domain scopes are.
HEALTH_TOKEN = SessionContext(principal_kind="capability_token", domain_scopes=("health",))

CONV_COUNT = "SELECT count(*) FROM app.note_conversations WHERE session_id = CAST(:id AS uuid)"
CALL_COUNT = (
    "SELECT count(*) FROM app.note_conversation_tool_calls WHERE session_id = CAST(:id AS uuid)"
)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def owner_ctx(maker: async_sessionmaker) -> SessionContext:
    """A real owner principal — `agent_sessions.principal_id` is a FK, so the random id
    on the shared OWNER context will not do."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def narrowed(owner: SessionContext) -> SessionContext:
    """The same owner, firewalled to `health` — a note conversation's own runtime shape
    (constraint 2 runs it owner-scoped to the note's domain plus `general`)."""
    return SessionContext(
        principal_id=owner.principal_id,
        principal_kind="owner",
        owner_scoped=True,
        domain_scopes=("health",),
    )


async def seed_conversation(maker: async_sessionmaker, owner: SessionContext) -> tuple[str, str]:
    """A `general` note, a session, a conversation and one ledger row. Returns
    (session_id, note_id)."""
    sid, nid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (CAST(:id AS uuid), :cid, 'general', 'conversation seed note')"
            ),
            {"id": nid, "cid": f"conv-{nid[:13]}"},
        )
        await s.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (CAST(:id AS uuid), :pid, 'curator', '{general}')"
            ),
            {"id": sid, "pid": owner.principal_id},
        )
        await s.execute(
            text(
                "INSERT INTO app.note_conversations (session_id, note_id, state, note_body_sha)"
                " VALUES (CAST(:sid AS uuid), CAST(:nid AS uuid), 'waiting_on_owner', 'abc123')"
            ),
            {"sid": sid, "nid": nid},
        )
        await s.execute(
            text(
                "INSERT INTO app.note_conversation_tool_calls"
                " (session_id, name, args, ok, detail, domains)"
                " VALUES (CAST(:sid AS uuid), 'assert_fact', '{\"quote\": \"x\"}', true,"
                " 'written', ARRAY['health'])"
            ),
            {"sid": sid},
        )
    return sid, nid


async def visible(maker: async_sessionmaker, ctx: SessionContext, sql: str, sid: str) -> int:
    async with scoped_session(maker, ctx) as s:
        return int((await s.execute(text(sql), {"id": sid})).scalar_one())


async def test_both_tables_are_owner_only(maker: async_sessionmaker) -> None:
    owner = await owner_ctx(maker)
    sid, _ = await seed_conversation(maker, owner)
    for sql in (CONV_COUNT, CALL_COUNT):
        assert await visible(maker, owner, sql, sid) == 1
        assert await visible(maker, HEALTH_TOKEN, sql, sid) == 0
        assert await visible(maker, UNSCOPED, sql, sid) == 0


async def test_a_narrowed_owner_still_sees_its_own_threads(maker: async_sessionmaker) -> None:
    """The inbox must not lose a question to the scope of whatever session renders it.
    The NOTE stays firewalled — that is the whole reason the conversation row carries no
    domain of its own."""
    owner = await owner_ctx(maker)
    sid, nid = await seed_conversation(maker, owner)
    health = narrowed(owner)

    assert await visible(maker, health, CONV_COUNT, sid) == 1
    assert await visible(maker, health, CALL_COUNT, sid) == 1
    async with scoped_session(maker, health) as s:
        notes = (
            await s.execute(
                text("SELECT count(*) FROM app.notes WHERE id = CAST(:id AS uuid)"), {"id": nid}
            )
        ).scalar_one()
    assert notes == 0  # a `general` note is out of a health-narrowed session's scope


async def test_a_token_session_cannot_open_a_conversation(maker: async_sessionmaker) -> None:
    """The write side is the one that matters: a conversation drives graph writes, so
    nothing without owner identity may open one or file a ledger row."""
    owner = await owner_ctx(maker)
    sid, nid = await seed_conversation(maker, owner)

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, HEALTH_TOKEN) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_conversations (session_id, note_id, note_body_sha)"
                    " VALUES (CAST(:sid AS uuid), CAST(:nid AS uuid), 'sneaky')"
                ),
                {"sid": str(uuid.uuid4()), "nid": nid},
            )
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, HEALTH_TOKEN) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_conversation_tool_calls (session_id, name, ok)"
                    " VALUES (CAST(:sid AS uuid), 'assert_fact', true)"
                ),
                {"sid": sid},
            )


async def test_an_unscoped_session_is_refused(maker: async_sessionmaker) -> None:
    owner = await owner_ctx(maker)
    sid, nid = await seed_conversation(maker, owner)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, UNSCOPED) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_conversations (session_id, note_id, note_body_sha)"
                    " VALUES (CAST(:sid AS uuid), CAST(:nid AS uuid), 'nope')"
                ),
                {"sid": str(uuid.uuid4()), "nid": nid},
            )
    assert await visible(maker, UNSCOPED, CONV_COUNT, sid) == 0


async def test_a_conversation_row_cannot_be_deleted_on_its_own(maker: async_sessionmaker) -> None:
    """No DELETE grant. Erasing the side row would leave the `agent_sessions` row and a
    transcript full of the note's body — so a conversation only ever dies with its
    session, which is what `analysis/purge.py` deletes."""
    owner = await owner_ctx(maker)
    sid, _ = await seed_conversation(maker, owner)
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, owner) as s:
            await s.execute(
                text("DELETE FROM app.note_conversations WHERE session_id = CAST(:id AS uuid)"),
                {"id": sid},
            )
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, owner) as s:
            await s.execute(
                text(
                    "DELETE FROM app.note_conversation_tool_calls"
                    " WHERE session_id = CAST(:id AS uuid)"
                ),
                {"id": sid},
            )


async def test_a_ledger_row_takes_only_a_turn_binding(maker: async_sessionmaker) -> None:
    """`GRANT UPDATE (turn_id)` and nothing more: a recorded call is what happened, and
    the only late-arriving fact about it is which assistant turn it belongs to."""
    owner = await owner_ctx(maker)
    sid, _ = await seed_conversation(maker, owner)
    turn = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, role, content)"
                " VALUES (CAST(:tid AS uuid), CAST(:sid AS uuid), 'assistant', 'done')"
            ),
            {"tid": turn, "sid": sid},
        )
        await s.execute(
            text(
                "UPDATE app.note_conversation_tool_calls SET turn_id = CAST(:tid AS uuid)"
                " WHERE session_id = CAST(:sid AS uuid)"
            ),
            {"tid": turn, "sid": sid},
        )

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, owner) as s:
            await s.execute(
                text(
                    "UPDATE app.note_conversation_tool_calls SET detail = 'rewritten'"
                    " WHERE session_id = CAST(:sid AS uuid)"
                ),
                {"sid": sid},
            )


async def test_deleting_the_session_cascades_both_tables(maker: async_sessionmaker) -> None:
    """The only removal path there is — and the one `analysis/purge.py` uses, since the
    note FK's cascade never fires on a soft delete."""
    owner = await owner_ctx(maker)
    sid, _ = await seed_conversation(maker, owner)
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text("DELETE FROM app.agent_sessions WHERE id = CAST(:id AS uuid)"), {"id": sid}
        )
    assert await visible(maker, owner, CONV_COUNT, sid) == 0
    assert await visible(maker, owner, CALL_COUNT, sid) == 0
