"""`NoteConversationRepo` against real Postgres (migration 0191).

The behaviour that lives in SQL rather than in Python: the one-live-conversation partial
unique index and which states it treats as live, the ledger's `seq` ordering and its
late `turn_id` binding, and the whole-conversation `touched`/`projected` union that
constraint 6's settle sweep reads back.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.note_conversation import NoteConversationRepo, note_body_sha
from tests.conftest import docker_available
from tests.integration.test_note_conversation_rls import owner_ctx
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


@pytest.fixture
async def owner(maker: async_sessionmaker) -> SessionContext:
    return await owner_ctx(maker)


async def seed_note(maker: async_sessionmaker, owner: SessionContext) -> str:
    nid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (CAST(:id AS uuid), :cid, 'general', 'repo seed note')"
            ),
            {"id": nid, "cid": f"convrepo-{nid[:13]}"},
        )
    return nid


async def seed_session(maker: async_sessionmaker, owner: SessionContext) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (CAST(:id AS uuid), :pid, 'curator', '{general}')"
            ),
            {"id": sid, "pid": owner.principal_id},
        )
    return sid


async def test_start_and_find_the_live_conversation(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)

    async with scoped_session(maker, owner) as s:
        assert await repo.live_for_note(s, note) is None
        conv = await repo.start(s, session_id=sid, note_id=note, body_sha=note_body_sha("hello"))
        assert conv.state == "running"

    async with scoped_session(maker, owner) as s:
        live = await repo.live_for_note(s, note)
        assert live is not None and str(live.session_id) == sid
        assert live.note_body_sha == note_body_sha("hello")
        assert (await repo.get(s, sid)) is not None


@pytest.mark.parametrize("state", ["running", "waiting_on_owner"])
async def test_a_second_live_conversation_for_a_note_is_refused(
    maker: async_sessionmaker, owner: SessionContext, state: str
) -> None:
    """Both live states hold the note. `waiting_on_owner` especially: it is the thread
    the inbox points at, so a rival would orphan the question the owner is answering."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    first, second = await seed_session(maker, owner), await seed_session(maker, owner)

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=first, note_id=note, body_sha="sha", state=state)

    with pytest.raises(IntegrityError, match="note_conversations_one_live"):
        async with scoped_session(maker, owner) as s:
            await repo.start(s, session_id=second, note_id=note, body_sha="sha")


@pytest.mark.parametrize("state", ["settled", "failed"])
async def test_an_ended_conversation_releases_the_note(
    maker: async_sessionmaker, owner: SessionContext, state: str
) -> None:
    """A settled thread is done and a failed pass must be retryable — neither may hold
    the note hostage on a box the owner cannot shell into."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    first, second = await seed_session(maker, owner), await seed_session(maker, owner)

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=first, note_id=note, body_sha="sha")
        moved = await repo.set_state(s, first, state)
        assert moved is not None and moved.state == state
        assert await repo.live_for_note(s, note) is None

    async with scoped_session(maker, owner) as s:
        conv = await repo.start(s, session_id=second, note_id=note, body_sha="sha2")
        assert conv.state == "running"


async def test_an_unknown_state_is_refused_by_the_check(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """The closed set is Postgres', not the repo's — one authority, no drift."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
    with pytest.raises(IntegrityError):
        async with scoped_session(maker, owner) as s:
            await repo.set_state(s, sid, "hibernating")


async def test_waiting_threads_list_newest_first(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """The notes inbox tab's query (D4/D5) — a queryable state, not a derived guess."""
    repo = NoteConversationRepo()
    waiting: list[str] = []
    for _ in range(3):
        note = await seed_note(maker, owner)
        sid = await seed_session(maker, owner)
        async with scoped_session(maker, owner) as s:
            await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
            await repo.set_state(s, sid, "waiting_on_owner")
        waiting.append(sid)
    # A settled thread of its own note must not appear in the inbox.
    quiet_note, quiet = await seed_note(maker, owner), await seed_session(maker, owner)
    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=quiet, note_id=quiet_note, body_sha="sha")
        await repo.set_state(s, quiet, "settled")

    async with scoped_session(maker, owner) as s:
        rows = await repo.list_in_state(s, "waiting_on_owner")
    listed = [str(r.session_id) for r in rows]
    assert quiet not in listed
    # The shared test database carries other suites' rows, so assert on order among ours.
    assert [sid for sid in listed if sid in waiting] == list(reversed(waiting))


async def test_the_ledger_records_calls_in_order_and_binds_its_turn(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    entity, fact = uuid.uuid4(), uuid.uuid4()

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        await repo.record_tool_call(
            s,
            sid,
            name="resolve_entity",
            args={"surfaces": ["Kaiya"]},
            ok=True,
            entity_ids=[entity],
            domains=["general"],
        )
        await repo.record_tool_call(
            s,
            sid,
            name="assert_fact",
            args={"quote": "she saw Dr Patel"},
            ok=True,
            entity_ids=[entity],
            fact_ids=[fact],
            domains=["health"],
            detail="written",
        )
        await repo.record_tool_call(
            s,
            sid,
            name="assert_fact",
            args={"quote": "junk"},
            ok=False,
            fact_ids=[uuid.uuid4()],
            domains=["finance"],
            detail="no such handle",
        )

    async with scoped_session(maker, owner) as s:
        calls = await repo.tool_calls(s, sid)
    assert [c.name for c in calls] == ["resolve_entity", "assert_fact", "assert_fact"]
    assert [c.seq for c in calls] == sorted(c.seq for c in calls)
    assert all(c.turn_id is None for c in calls)  # recorded before the turn exists

    turn = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, role, content)"
                " VALUES (CAST(:tid AS uuid), CAST(:sid AS uuid), 'assistant', 'done')"
            ),
            {"tid": turn, "sid": sid},
        )
        await repo.bind_turn(s, sid, turn)
        # A later call belongs to the NEXT turn and must not be re-attributed.
        await repo.record_tool_call(s, sid, name="ask_owner", ok=True)

    async with scoped_session(maker, owner) as s:
        calls = await repo.tool_calls(s, sid)
    assert [str(c.turn_id) if c.turn_id else None for c in calls] == [turn, turn, turn, None]


async def test_writes_accumulate_across_turns_and_skip_failures(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """Constraint 6: `touched`/`projected` must be the union over the WHOLE conversation,
    or the settle sweep retracts the previous turn's commits. A failed call asserted
    nothing, so its ids must not spare a fact from that sweep."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    fact_a, fact_b, ghost = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    entity = uuid.uuid4()

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        await repo.record_tool_call(
            s,
            sid,
            name="assert_fact",
            ok=True,
            fact_ids=[fact_a],
            entity_ids=[entity],
            domains=["general"],
        )
    # A separate transaction: the second turn of the same conversation.
    async with scoped_session(maker, owner) as s:
        await repo.record_tool_call(
            s,
            sid,
            name="assert_fact",
            ok=True,
            fact_ids=[fact_b],
            entity_ids=[entity],
            domains=["health"],
        )
        await repo.record_tool_call(s, sid, name="assert_fact", ok=False, fact_ids=[ghost])

    async with scoped_session(maker, owner) as s:
        written = await repo.writes(s, sid)
    assert written.facts == {fact_a, fact_b}
    assert written.entities == {entity}
    assert written.domains == {"general", "health"}


async def test_oversized_args_are_capped_not_refused(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """A hostile note body must not be able to grow the ledger without bound — and the
    cap must never cost the audit row for a call that already wrote to the graph."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        await repo.record_tool_call(
            s, sid, name="assert_fact", ok=True, args={"facts": [{"quote": "x" * 100_000}]}
        )

    async with scoped_session(maker, owner) as s:
        (call,) = await repo.tool_calls(s, sid)
    assert call.args["_truncated"] is True
    assert len(call.args["facts"][0]["quote"]) == 2000
