"""`ask_owner` against real Postgres: the question, the stop, and the answer coming back.

W3/T2b of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md. The LLM is faked (CLAUDE.md #5)
but the loop, the registry, the tool sidecar, the conversation tables and the note
clarification path are all real — which is the point: the three things this task claims
are properties of code that ran, not of prose.

What each test defends:

- **the ask is durable at ask time.** The question and the `waiting_on_owner` state land
  in ONE transaction, from the handler, because the owner can answer before the runner's
  post-turn record ever executes. A question recorded later is a question the reply path
  cannot read.
- **"and stop" is enforced by the loop, not asked for in prose.** The scripted model gets
  exactly one turn; a second model call would exhaust the fake and raise. TOOL_SURFACE.md
  is explicit that gpt-oss does not honour protocol obligations stated in prose.
- **a waiting thread never settles** (constraint 6). The settle sweep retracts every
  non-pinned fact a `settled` pass did not vouch for, and a pass that stopped to ask has
  read only half the note.
- **the answer becomes part of the NOTE** (D6/D7), as a timestamped block, and the block
  enqueues its own re-ingest so the graph re-derives from notes alone.
- **`note_body_sha` finally has a reader.** It is compared before the append and
  re-stamped only when it matched, so "the note moved under this thread" stays a true
  statement about something OTHER than this thread's own answer.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.agent.asktools import ASK_OWNER_TOOL, TOOLS_DIR, build_ask_owner_handlers
from jbrain.agent.graphwritetools import note_registry
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.analysis.clarify import close_owner_reply, record_owner_reply
from jbrain.analysis.converse import NOTE_CONVERSE_AGENT, NoteConverseRunner
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, ToolCall
from jbrain.models.note_conversation import (
    AWAITING_OWNER,
    InvalidStateTransition,
    NoteConversationRepo,
    note_body_sha,
)
from jbrain.notes.repo import SqlNotesRepo
from jbrain.tasks.runner import LoopTurnExecutor
from tests.conftest import docker_available
from tests.integration.test_note_conversation_rls import owner_ctx
from tests.integration.test_note_converse_pg import BrokenTranscript
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

NOTE_BODY = "Ran the 10k with Sarah this morning. She has a new coach."
QUESTION = "Which Sarah is this — your sister, or Sarah Chen from the running club?"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def owner(maker: async_sessionmaker[AsyncSession]) -> SessionContext:
    return await owner_ctx(maker)


def _const(value: str):  # noqa: ANN202
    async def _get() -> str:
        return value

    return _get


async def _note(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, body: str = NOTE_BODY
) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        owner, client_id=f"ask-{uuid.uuid4()}", domain="general", destination=None, body=body
    )
    return note.id


async def _conversation(
    maker: async_sessionmaker[AsyncSession],
    owner: SessionContext,
    note_id: str,
    *,
    state: str = "running",
) -> str:
    """A note conversation the way the runner opens one: a dedicated session row plus the
    side table that makes it a note conversation."""
    session = await AgentSessionRepo(maker).create(
        owner, domain_scopes=[], title=f"note {note_id[:8]}", agent=NOTE_CONVERSE_AGENT
    )
    note = await SqlNotesRepo(maker).get_note(owner, note_id)
    assert note is not None
    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().start(
            s,
            session_id=session.id,
            note_id=note_id,
            body_sha=note_body_sha(note.body),
            state=state,
        )
    return session.id


def _ctx(owner: SessionContext, session_id: str | None) -> ToolContext:
    return ToolContext(session=owner, scopes=("general",), agent_session_id=session_id)


async def _state(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, session_id: str
) -> tuple[str, str]:
    async with scoped_session(maker, owner) as s:
        row = await NoteConversationRepo().get(s, session_id)
        assert row is not None
        return row.state, row.note_body_sha


async def _ledger(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, session_id: str
) -> list[Any]:
    async with scoped_session(maker, owner) as s:
        return await NoteConversationRepo().tool_calls(s, session_id)


# --- the ask ------------------------------------------------------------------


async def test_the_question_and_the_wait_land_together(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """One transaction for both. The owner can reply before the runner's post-turn record
    runs, and the reply path builds the clarification block from the recorded question —
    so a state that says "waiting" over a ledger that holds no question is a wait nobody
    can explain, and a question with no wait is one nobody is asked."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    handler = build_ask_owner_handlers(maker)[ASK_OWNER_TOOL]

    out = await handler({"question": QUESTION}, _ctx(owner, session_id))

    assert isinstance(out, ToolOutput)
    assert out.halt == AWAITING_OWNER
    state, _ = await _state(maker, owner, session_id)
    assert state == "waiting_on_owner"
    (row,) = await _ledger(maker, owner, session_id)
    assert row.name == ASK_OWNER_TOOL
    assert row.args["question"] == QUESTION
    assert row.ok is True
    # It wrote no graph, and says so rather than defaulting to silence.
    assert row.domains == []
    assert row.entity_ids == []


async def test_a_second_question_is_refused_while_the_first_is_open(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """Two open questions would leave the reply path guessing which one the owner's next
    message answers — and that answer becomes source text on the note, so the wrong
    pairing is a wrong sentence in the corpus."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    handler = build_ask_owner_handlers(maker)[ASK_OWNER_TOOL]
    await handler({"question": QUESTION}, _ctx(owner, session_id))

    second = await handler({"question": "And which coach?"}, _ctx(owner, session_id))

    assert not isinstance(second, ToolOutput)  # no halt: the turn was not ended again
    assert "already waiting" in second
    assert QUESTION in second
    assert len(await _ledger(maker, owner, session_id)) == 1


async def test_a_blank_question_records_nothing_and_does_not_stop_the_turn(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    handler = build_ask_owner_handlers(maker)[ASK_OWNER_TOOL]

    out = await handler({"question": "   "}, _ctx(owner, session_id))

    assert not isinstance(out, ToolOutput)
    assert await _ledger(maker, owner, session_id) == []
    state, _ = await _state(maker, owner, session_id)
    assert state == "running"


async def test_outside_a_note_conversation_it_refuses_in_words(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The `note_ingest` allowlist is the boundary, but the handler is reachable from the
    /chat registry (the owner's REPLY is an ordinary chat turn), so a session with no
    conversation behind it has to refuse rather than raise — loop.py turns a raised
    exception into a generic error the model learns nothing from."""
    handler = build_ask_owner_handlers(maker)[ASK_OWNER_TOOL]
    orphan = await AgentSessionRepo(maker).create(owner, domain_scopes=[], title="chat")

    assert "only inside a note's conversation" in await handler(
        {"question": QUESTION}, _ctx(owner, None)
    )
    assert "only inside a note's conversation" in await handler(
        {"question": QUESTION}, _ctx(owner, orphan.id)
    )


async def test_a_waiting_conversation_cannot_be_settled(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """Constraint 6, at the only place that can enforce it. `settled` is what the
    whole-note settle sweep fires on, and it means "this pass wrote everything it meant
    to" — a pass that stopped to ask a question has not."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {"question": QUESTION}, _ctx(owner, session_id)
    )

    async with scoped_session(maker, owner) as s:
        with pytest.raises(InvalidStateTransition):
            await NoteConversationRepo().set_state(s, session_id, "settled")


# --- and stop -----------------------------------------------------------------


async def test_the_turn_ends_on_the_ask_and_the_note_waits(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The whole unattended path, through the REAL loop and the REAL registry: the model
    calls `ask_owner`, and the turn is over.

    Exactly one model turn is scripted. `FakeLlmClient` raises when the loop asks for a
    second, so "the turn ended here" is asserted by the test failing loudly if it did
    not — which is the only way to assert it, since prose in the tool description cannot
    make it true (TOOL_SURFACE.md)."""
    note_id = await _note(maker, owner)
    fake = FakeLlmClient(
        turns=[
            LlmTurn(
                "",
                (ToolCall("c1", ASK_OWNER_TOOL, {"question": QUESTION}),),
                "tool_use",
                LlmUsage(10, 3),
            )
        ]
    )
    router = LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")})
    runner = NoteConverseRunner(
        maker,
        notes=SqlNotesRepo(maker),
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=AgentTranscript(maker),
        executor=LoopTurnExecutor(
            router, note_registry(TOOLS_DIR, build_ask_owner_handlers(maker))
        ),
        owner_principal_id=_const(owner.principal_id),
    )

    await runner.note_converse({"note_id": note_id})

    async with scoped_session(maker, owner) as s:
        row = (
            await s.execute(
                text(
                    "SELECT session_id::text AS sid, state FROM app.note_conversations"
                    " WHERE note_id = CAST(:n AS uuid)"
                ),
                {"n": note_id},
            )
        ).one()
    assert row.state == "waiting_on_owner"
    # The question is on the ledger ONCE — the handler wrote it, and the runner's
    # post-turn recorder knows not to write it again.
    calls = await _ledger(maker, owner, row.sid)
    assert [c.name for c in calls] == [ASK_OWNER_TOOL]
    assert calls[0].args["question"] == QUESTION
    # And the run is closed out as a real, finished run rather than an error.
    async with scoped_session(maker, owner) as s:
        status, stop = (
            await s.execute(
                text(
                    "SELECT status, stop_reason FROM app.runs WHERE session_id = CAST(:i AS uuid)"
                ),
                {"i": row.sid},
            )
        ).one()
    assert (status, stop) == ("done", AWAITING_OWNER)


async def test_a_failure_after_the_ask_does_not_retract_the_question(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The ask is durable; a later fault in the same pass is not grounds to drop it.

    The handler moved the thread to `waiting_on_owner` inside the tool call, and the
    owner may already be typing. The runner's own settle would write `failed` here — a
    transition `_ALLOWED_SOURCES` refuses without `abandon_question=True`, precisely so a
    failure that means nothing of the sort cannot silently drop the owner's question (and
    which would otherwise leave the job raising and retrying)."""
    note_id = await _note(maker, owner)
    fake = FakeLlmClient(
        turns=[
            LlmTurn(
                "",
                (ToolCall("c1", ASK_OWNER_TOOL, {"question": QUESTION}),),
                "tool_use",
                LlmUsage(10, 3),
            )
        ]
    )
    runner = NoteConverseRunner(
        maker,
        notes=SqlNotesRepo(maker),
        sessions=AgentSessionRepo(maker),
        runlog=AgentRunLog(maker),
        transcript=BrokenTranscript(maker),
        executor=LoopTurnExecutor(
            LlmRouter({"xai": fake}, {"agent.turn": ("xai", "grok-4.3")}),
            note_registry(TOOLS_DIR, build_ask_owner_handlers(maker)),
        ),
        owner_principal_id=_const(owner.principal_id),
    )

    await runner.note_converse({"note_id": note_id})

    async with scoped_session(maker, owner) as s:
        row = (
            await s.execute(
                text(
                    "SELECT session_id::text AS sid, state FROM app.note_conversations"
                    " WHERE note_id = CAST(:n AS uuid)"
                ),
                {"n": note_id},
            )
        ).one()
    assert row.state == "waiting_on_owner"
    # And the question the owner is waiting on is still readable, which is what makes
    # the wait answerable at all.
    calls = await _ledger(maker, owner, row.sid)
    assert [c.args["question"] for c in calls] == [QUESTION]


# --- the owner answers --------------------------------------------------------


async def test_the_reply_becomes_a_dated_block_on_the_note_and_re_ingests_it(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """D6 + D7 in one chain: the answer is appended to the note as a timestamped block,
    the note's text now carries it, and `ingest_note` is queued so the block becomes
    chunks of the same note and the graph re-derives from notes alone."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {"question": QUESTION}, _ctx(owner, session_id)
    )

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )

    assert reply is not None
    assert reply.clarified is True
    assert reply.question == QUESTION
    assert reply.note_moved is False
    note = await SqlNotesRepo(maker).get_note(owner, note_id)
    assert note is not None
    # The body the author wrote is still the head of the note, byte for byte (D6's
    # freeze), and the block is appended after it.
    assert note.body.startswith(NOTE_BODY)
    assert f"Q: {QUESTION}" in note.body
    assert "A: My sister." in note.body
    assert "[clarification " in note.body
    # The re-ingest is queued by the append itself, in its own transaction.
    async with scoped_session(maker, owner) as s:
        queued = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ingest_note'"
                    " AND payload->>'note_id' = :n"
                ),
                {"n": note_id},
            )
        ).scalar_one()
    assert queued == 1
    # The thread is live again, and the sha now names the text the thread stands on:
    # it asked the question and read the answer, so it has seen every character of it.
    state, sha = await _state(maker, owner, session_id)
    assert state == "running"
    assert sha == note_body_sha(note.body)


async def test_a_reply_into_a_thread_that_is_not_waiting_is_only_conversation(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """`note_clarifications.question` is NOT NULL and non-blank in Postgres, so there is
    no shape for "the owner said something unprompted" — and there should not be: a note
    is not a chat log. An unprompted message stays chat."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id, state="running")

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="Actually it was a 5k.",
    )

    assert reply is None
    note = await SqlNotesRepo(maker).get_note(owner, note_id)
    assert note is not None and note.body == NOTE_BODY


async def test_a_note_that_moved_under_the_thread_keeps_its_stale_sha(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """`note_body_sha` shipped in W2 with no reader; this is the reader. When something
    OTHER than this thread changed the note, the answer is still appended — the owner
    answered the question that was asked — but the sha is left stale, so the field keeps
    saying the true thing: this conversation has not read the note as it now stands."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {"question": QUESTION}, _ctx(owner, session_id)
    )
    _, sha_at_ask = await _state(maker, owner, session_id)
    # The owner edits the note in the PWA while the question sits in the thread.
    from jbrain.notes.service import NoteUpdate

    await SqlNotesRepo(maker).update_note(
        owner, note_id, NoteUpdate(body="Ran the 10k with Sarah. She has a new coach and a PB.")
    )

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )

    assert reply is not None and reply.clarified is True
    assert reply.note_moved is True
    note = await SqlNotesRepo(maker).get_note(owner, note_id)
    assert note is not None and "A: My sister." in note.body
    _, sha_now = await _state(maker, owner, session_id)
    assert sha_now == sha_at_ask
    assert sha_now != note_body_sha(note.body)


async def test_the_answer_is_recorded_once_even_if_the_owner_says_it_twice(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The state transition is the latch, and it happens BEFORE the append: the second
    message finds a thread that is no longer waiting, so the note cannot collect the same
    answer twice as source text — which, with no per-block eraser in the PWA, is the one
    of the two failure directions the owner could not undo."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {"question": QUESTION}, _ctx(owner, session_id)
    )
    notes = SqlNotesRepo(maker)

    first = await record_owner_reply(
        maker, notes, owner, session_id=session_id, agent=NOTE_CONVERSE_AGENT, message="My sister."
    )
    second = await record_owner_reply(
        maker, notes, owner, session_id=session_id, agent=NOTE_CONVERSE_AGENT, message="My sister."
    )

    assert first is not None and first.clarified is True
    assert second is None
    note = await notes.get_note(owner, note_id)
    assert note is not None and note.body.count("A: My sister.") == 1


async def test_a_server_authored_outcome_is_never_filed_as_the_owners_answer(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """A `proposal_outcome` / `deferred_outcome` turn carries text the SERVER wrote — an
    enact summary, a finished off-turn analysis — framed as a DATA report, which is why
    the transcript already declines to record one as a user turn.

    Filing one here would be the wrong-sentence-in-the-corpus failure this module names:
    it pairs machine prose with the agent's open question, appends the pair to Jeff's own
    note as SOURCE text (chunked, embedded, citable), and spends the question so his real
    answer can never be paired. Nothing moves, and the thread stays waiting — the truth,
    since nobody has answered yet."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {"question": QUESTION}, _ctx(owner, session_id)
    )
    notes = SqlNotesRepo(maker)

    outcome = await record_owner_reply(
        maker,
        notes,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="Enacted 1 of 1 — 1 approved, 0 held.",
        owner_authored=False,
    )

    assert outcome is None
    note = await notes.get_note(owner, note_id)
    assert note is not None and note.body == NOTE_BODY
    async with scoped_session(maker, owner) as s:
        queued = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ingest_note'"
                    " AND payload->>'note_id' = :n"
                ),
                {"n": note_id},
            )
        ).scalar_one()
    assert queued == 0
    # The question is NOT consumed: the thread still waits, so the owner's real answer
    # still has something to be paired with.
    state, _ = await _state(maker, owner, session_id)
    assert state == "waiting_on_owner"

    answer = await record_owner_reply(
        maker,
        notes,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )

    assert answer is not None and answer.clarified is True and answer.question == QUESTION
    note = await notes.get_note(owner, note_id)
    assert note is not None
    assert "A: My sister." in note.body
    assert "Enacted 1 of 1" not in note.body


async def test_a_reply_to_another_persona_s_chat_is_not_touched(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    session = await AgentSessionRepo(maker).create(owner, domain_scopes=[], title="chat")

    assert (
        await record_owner_reply(
            maker,
            SqlNotesRepo(maker),
            owner,
            session_id=session.id,
            agent="curator",
            message="hello",
        )
        is None
    )


# --- and the reply turn closes ------------------------------------------------


async def test_the_reply_turn_closes_the_thread_it_reopened(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """`running` holds the note's one live slot, and the re-ingest the answer queued
    emits its own `note.ingested` — a reply turn that never closed would leave the
    answered note suppressed until the stale-conversation reaper called it `failed`."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {"question": QUESTION}, _ctx(owner, session_id)
    )
    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )

    await close_owner_reply(
        maker,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        stop_reason="end_turn",
        reopened=reply is not None,
    )

    state, _ = await _state(maker, owner, session_id)
    assert state == "settled"


async def test_a_reply_turn_that_asked_again_is_left_waiting(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The agent may still be unable to proceed after the answer. The close must not
    overwrite the new question: the handler already put the thread back in
    `waiting_on_owner`, and `state_for_stop` agrees."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    handler = build_ask_owner_handlers(maker)[ASK_OWNER_TOOL]
    await handler({"question": QUESTION}, _ctx(owner, session_id))
    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )
    await handler({"question": "Which running club?"}, _ctx(owner, session_id))

    await close_owner_reply(
        maker,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        stop_reason=AWAITING_OWNER,
        reopened=reply is not None,
    )

    state, _ = await _state(maker, owner, session_id)
    assert state == "waiting_on_owner"
    assert [c.args["question"] for c in await _ledger(maker, owner, session_id)] == [
        QUESTION,
        "Which running club?",
    ]


async def test_a_reply_turn_that_died_releases_the_note(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """A Stop, a dropped connection or a mid-turn error still has to release the live
    slot — the close runs in the turn's `finally` for exactly this."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {"question": QUESTION}, _ctx(owner, session_id)
    )
    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )

    await close_owner_reply(
        maker,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        stop_reason="disconnected",
        reopened=reply is not None,
    )

    state, _ = await _state(maker, owner, session_id)
    assert state == "failed"


async def test_a_chat_turn_during_the_worker_pass_does_not_end_it(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The close needs a POSITIVE signal, not a `running` state.

    A conversation is `running` for the whole unattended pass — up to
    `NOTE_TURN_WALL_CLOCK`, 30 minutes — and `/chat`'s busy guard counts only the API's
    own live turns, so nothing stops the owner opening the thread and typing while the
    worker's pass is mid-flight. `record_owner_reply` correctly declines (the thread is
    not waiting on anything, so there is no question to pair the message with), and the
    close must decline with it: settling here would declare a LIVE pass finished before
    its `_record` wrote a ledger row, and the settle behind the close would then sweep
    the note against an empty ledger. It also left the worker's own `set_state` raising
    `InvalidStateTransition` into a job retry, which opens a second thread for the note.
    """
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)  # opens `running`

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="just a thought while you read",
    )
    assert reply is None, "nothing was waiting, so nothing was answered"

    closed = await close_owner_reply(
        maker,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        stop_reason="end_turn",
        reopened=reply is not None,
    )

    assert closed is None, "the chat turn ended a pass it had no part in"
    state, _ = await _state(maker, owner, session_id)
    assert state == "running"
