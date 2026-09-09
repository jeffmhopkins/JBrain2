"""`NoteConversationRepo` against real Postgres (migration 0191).

The behaviour that lives in SQL rather than in Python: the one-live-conversation partial
unique index and which states it treats as live, the ledger's `seq` ordering and its
late `turn_id` binding, and the whole-conversation `touched`/`projected` union that
constraint 6's settle sweep reads back.

Plus the three promises the pure functions cannot make on their own — that a capped
`args` blob really survives the JSONB bind processor, that every read path is scoped to
one session (which is what 0191's owner-only RLS argument rests on), and that the
lifecycle refuses the edges that would silently drop the owner's question.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.note_conversation import (
    InvalidStateTransition,
    NoteConversationRepo,
    note_body_sha,
)
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


async def test_notes_inbox_lists_the_question_the_row_redirects_to(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """The notes tab's query (D4/D5): the LAST `ask_owner` of the thread, the note it
    came from, and the committed count — oldest wait first."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    fact_a, fact_b = uuid.uuid4(), uuid.uuid4()

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        await repo.record_tool_call(
            s, sid, name="assert_fact", ok=True, fact_ids=[fact_a, fact_b], domains=["general"]
        )
        # A failed call asserted nothing, so it must not inflate "how much landed".
        await repo.record_tool_call(
            s, sid, name="assert_fact", ok=False, fact_ids=[uuid.uuid4()], domains=[]
        )
        await repo.record_tool_call(
            s, sid, name="ask_owner", args={"question": "answered already"}, ok=True, domains=[]
        )
        await repo.record_tool_call(
            s, sid, name="ask_owner", args={"question": "which shop?"}, ok=True, domains=[]
        )
        await repo.set_state(s, sid, "waiting_on_owner")

    async with scoped_session(maker, owner) as s:
        rows = {r.session_id: r for r in await repo.notes_inbox(s)}

    row = rows[sid]
    assert row.question == "which shop?"
    assert row.note_id == note and row.domain == "general"
    assert row.note_excerpt == "repo seed note"
    assert row.committed == 2
    assert row.live is False
    assert row.agent == "curator"


async def test_notes_inbox_lists_a_first_pass_but_marks_it_live(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """A `running` conversation is listed so the note is visibly in hand — and flagged,
    because the route leaves it out of the count: nothing is waiting on the owner yet."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")

    async with scoped_session(maker, owner) as s:
        row = {r.session_id: r for r in await repo.notes_inbox(s)}[sid]
    assert row.live is True and row.question is None


async def test_notes_inbox_drops_a_settled_thread_and_a_deleted_note(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """A settled thread is not waiting, and a soft-deleted note's thread is a redirect
    into a dead end — `notes/repo.py`'s delete is soft, so its conversation survives."""
    repo = NoteConversationRepo()
    settled_note, deleted_note = await seed_note(maker, owner), await seed_note(maker, owner)
    settled_sid, deleted_sid = await seed_session(maker, owner), await seed_session(maker, owner)

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=settled_sid, note_id=settled_note, body_sha="sha")
        await repo.set_state(s, settled_sid, "settled")
        await repo.start(s, session_id=deleted_sid, note_id=deleted_note, body_sha="sha")
        await repo.set_state(s, deleted_sid, "waiting_on_owner")
        await s.execute(
            text("UPDATE app.notes SET deleted_at = now() WHERE id = CAST(:id AS uuid)"),
            {"id": deleted_note},
        )

    async with scoped_session(maker, owner) as s:
        listed = {r.session_id for r in await repo.notes_inbox(s)}
    assert settled_sid not in listed
    assert deleted_sid not in listed


async def test_the_ledger_records_calls_in_order_and_binds_its_turn(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    entity, fact = uuid.uuid4(), uuid.uuid4()

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        first = await repo.record_tool_call(
            s,
            sid,
            name="resolve_entity",
            args={"surfaces": ["Kaiya"]},
            ok=True,
            entity_ids=[entity],
            domains=["general"],
        )
        second = await repo.record_tool_call(
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
        third = await repo.record_tool_call(
            s,
            sid,
            name="assert_fact",
            args={"quote": "junk"},
            ok=False,
            fact_ids=[uuid.uuid4()],
            domains=["finance"],
            detail="no such handle",
        )
        turn_one_calls = [first.id, second.id, third.id]

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
        await repo.bind_turn(s, sid, turn, call_ids=turn_one_calls)
        # A later call belongs to the NEXT turn and must not be re-attributed.
        await repo.record_tool_call(s, sid, name="ask_owner", ok=True, domains=[])

    async with scoped_session(maker, owner) as s:
        calls = await repo.tool_calls(s, sid)
    assert [str(c.turn_id) if c.turn_id else None for c in calls] == [turn, turn, turn, None]


async def test_an_interrupted_turns_calls_are_not_adopted_by_the_next_turn(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """Constraint 6 names the case: a turn cut off by `max_steps` or by consecutive tool
    errors (`loop.py:131-133`) asserted a prefix and wrote no assistant turn, so its
    calls stay unbound forever. Binding "every unbound row" therefore hands them to the
    NEXT exchange and the D3 chip renders that write under the wrong one."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)

    # Turn one: one call, then the turn dies. No assistant turn is written.
    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        orphan = await repo.record_tool_call(
            s, sid, name="assert_fact", ok=True, fact_ids=[uuid.uuid4()], domains=["general"]
        )

    # Turn two: one call, and this time an assistant turn.
    turn_two = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        second = await repo.record_tool_call(
            s, sid, name="assert_fact", ok=True, fact_ids=[uuid.uuid4()], domains=["general"]
        )
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, role, content)"
                " VALUES (CAST(:tid AS uuid), CAST(:sid AS uuid), 'assistant', 'second')"
            ),
            {"tid": turn_two, "sid": sid},
        )
        await repo.bind_turn(s, sid, turn_two, call_ids=[second.id])

    async with scoped_session(maker, owner) as s:
        by_id = {c.id: c for c in await repo.tool_calls(s, sid)}
    assert by_id[orphan.id].turn_id is None
    assert str(by_id[second.id].turn_id) == turn_two


async def test_a_turn_binding_cannot_reach_another_conversation(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """`session_id` stays in the predicate, so a stray id cannot bind a stranger's row."""
    repo = NoteConversationRepo()
    mine, theirs = await seed_session(maker, owner), await seed_session(maker, owner)
    turn = str(uuid.uuid4())
    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=mine, note_id=await seed_note(maker, owner), body_sha="sha")
        await repo.start(
            s, session_id=theirs, note_id=await seed_note(maker, owner), body_sha="sha"
        )
        stranger = await repo.record_tool_call(
            s, theirs, name="assert_fact", ok=True, domains=["general"]
        )
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, role, content)"
                " VALUES (CAST(:tid AS uuid), CAST(:sid AS uuid), 'assistant', 'mine')"
            ),
            {"tid": turn, "sid": mine},
        )
        await repo.bind_turn(s, mine, turn, call_ids=[stranger.id])

    async with scoped_session(maker, owner) as s:
        (row,) = await repo.tool_calls(s, theirs)
    assert row.turn_id is None


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
        await repo.record_tool_call(
            s, sid, name="assert_fact", ok=False, fact_ids=[ghost], domains=[]
        )

    async with scoped_session(maker, owner) as s:
        written = await repo.writes(s, sid)
    assert written.facts == {fact_a, fact_b}
    assert written.entities == {entity}
    assert written.domains == {"general", "health"}
    # `frozen=True` only stops the fields being rebound; a caller that dropped an id
    # from a mutable set would silently widen the sweep, so the sets are frozen too.
    assert isinstance(written.facts, frozenset)
    assert isinstance(written.entities, frozenset)
    assert isinstance(written.domains, frozenset)


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
            s,
            sid,
            name="assert_fact",
            ok=True,
            args={"facts": [{"quote": "x" * 100_000}]},
            domains=["general"],
        )

    async with scoped_session(maker, owner) as s:
        (call,) = await repo.tool_calls(s, sid)
    assert call.args["_truncated"] is True
    assert len(call.args["facts"][0]["quote"]) == 2000


async def test_a_non_json_argument_does_not_abort_the_write(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """The cap exists so the ledger never costs a call its graph write — and the
    serializer is where that promise was actually broken. Nothing sets `json_serializer`
    on the engine, so SQLAlchemy's JSONB bind processor uses a bare `json.dumps` and a
    `UUID` (the most likely W3 arg shape) raised inside the flush. Proved end to end,
    because a pure-function assertion cannot see the bind processor."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    entity = uuid.uuid4()

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        await repo.record_tool_call(
            s,
            sid,
            name="assert_fact",
            args={"entity_id": entity, "at": datetime(2026, 9, 9, tzinfo=UTC)},
            ok=True,
            entity_ids=[entity],
            domains=["general"],
        )

    async with scoped_session(maker, owner) as s:
        (call,) = await repo.tool_calls(s, sid)
    assert call.args["entity_id"] == str(entity)


async def test_an_unknown_domain_code_is_refused_before_the_row_lands(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """`domains` cannot FK `app.domains(code)` and gets no trigger, so the repo boundary
    is the contract — a ledger that names a domain the firewall does not have is a
    ledger that lies about where the write went."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        with pytest.raises(ValueError, match="unknown domain code"):
            await repo.record_tool_call(s, sid, name="assert_fact", ok=True, domains=["medical"])

    async with scoped_session(maker, owner) as s:
        assert await repo.tool_calls(s, sid) == []


async def test_every_read_path_is_scoped_to_one_session(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """Owner-only RLS is the table's firewall, so this session predicate is the ONLY
    thing keeping one note's thread out of another's chip and settle set. 0191's
    docstring rests on it, so it is proved rather than asserted."""
    repo = NoteConversationRepo()
    mine, theirs = await seed_session(maker, owner), await seed_session(maker, owner)
    my_fact, their_fact = uuid.uuid4(), uuid.uuid4()

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=mine, note_id=await seed_note(maker, owner), body_sha="a")
        await repo.start(s, session_id=theirs, note_id=await seed_note(maker, owner), body_sha="b")
        await repo.record_tool_call(
            s, mine, name="assert_fact", ok=True, fact_ids=[my_fact], domains=["general"]
        )
        await repo.record_tool_call(
            s, theirs, name="assert_fact", ok=True, fact_ids=[their_fact], domains=["health"]
        )

    async with scoped_session(maker, owner) as s:
        calls = await repo.tool_calls(s, mine)
        written = await repo.writes(s, mine)
    assert [c.name for c in calls] == ["assert_fact"]
    assert {f for c in calls for f in c.fact_ids} == {my_fact}
    assert written.facts == frozenset({my_fact})
    assert written.domains == frozenset({"general"})


async def test_a_waiting_thread_cannot_be_failed_silently(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """The partial unique index blocks a RIVAL conversation, not a REPLACEMENT state. A
    retry or a reaper flipping `waiting_on_owner -> failed` drops the owner's question
    out of the notes tab and releases the note with no trace, so the edge needs saying
    out loud."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)

    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        await repo.set_state(s, sid, "waiting_on_owner")
        with pytest.raises(InvalidStateTransition, match="waiting_on_owner"):
            await repo.set_state(s, sid, "failed")

    async with scoped_session(maker, owner) as s:
        conv = await repo.get(s, sid)
        assert conv is not None and conv.state == "waiting_on_owner"
        # The named override is the way through, and the owner's reply is the other one.
        abandoned = await repo.set_state(s, sid, "failed", abandon_question=True)
    assert abandoned is not None and abandoned.state == "failed"


async def test_a_finished_thread_does_not_reopen(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """`settled` and `failed` released the note; a retry opens a fresh conversation
    rather than reviving one that already let go."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    async with scoped_session(maker, owner) as s:
        await repo.start(s, session_id=sid, note_id=note, body_sha="sha")
        await repo.set_state(s, sid, "settled")
        with pytest.raises(InvalidStateTransition, match="settled"):
            await repo.set_state(s, sid, "running")


async def test_a_missing_conversation_still_reads_as_gone(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """None means gone; the exception means refused. The two must not blur."""
    repo = NoteConversationRepo()
    async with scoped_session(maker, owner) as s:
        assert await repo.set_state(s, str(uuid.uuid4()), "settled") is None


async def test_a_conversation_cannot_be_opened_already_finished(
    maker: async_sessionmaker, owner: SessionContext
) -> None:
    """One opened straight into `settled` would release a note it never read, and the
    one-live index would not even notice."""
    repo = NoteConversationRepo()
    note = await seed_note(maker, owner)
    sid = await seed_session(maker, owner)
    async with scoped_session(maker, owner) as s:
        with pytest.raises(InvalidStateTransition, match="opens live"):
            await repo.start(s, session_id=sid, note_id=note, body_sha="sha", state="settled")
