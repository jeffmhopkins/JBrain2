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
- **one ask, several questions, one reply** (R1c of docs/plans/AGENT_INGEST_REWRITE.md).
  The set is durable with an id per question; one reply consumes the WHOLE set and files
  a block per answer with exactly ONE re-ingest; an answer naming a question that is not
  open is dropped; and what the owner left open comes back as `OwnerReply.unanswered`
  rather than being silently closed (O11 (ii)).
- **one reply consumes the set, and CONCURRENTLY.** The latch is a conditional UPDATE on
  `waiting_on_owner` (`claim_waiting`), not the state flip it used to be: the flip's
  allowed-sources table lets `running` follow `running`, so two overlapping replies both
  won it and the note gained the same answer twice. A sequential second reply cannot see
  that, so the test that pins it runs two in flight at once.
- **the two readers of the open set agree.** The notes tab and the reply path both take
  the newest SUCCEEDED `ask_owner` row and whatever it parses to — showing one set and
  consuming another files the owner's words against a question he was never shown.
"""

import asyncio
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

from jbrain.agent.asktools import (
    ASK_OWNER_TOOL,
    TOOLS_DIR,
    build_ask_owner_handlers,
    open_questions,
)
from jbrain.agent.graphwritetools import note_registry
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.analysis.clarify import (
    MAX_ANSWERS,
    close_owner_reply,
    owner_reply_notice,
    owner_turn_text,
    owner_words_reached_note,
    record_owner_reply,
)
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
COACH = "Which coach — the club's, or her own?"
DOSE = "Is the second line 25 mg or 2.5 mg?"


def _ask(*questions: str) -> dict[str, Any]:
    """The arguments one `ask_owner` call carries. R1c made this a SET: the model sends
    every question it is stuck on in one call, and the handler is what assigns each an
    id."""
    return {"questions": [{"question": q} for q in questions]}


def _asked(row: Any) -> list[str]:
    return [q["question"] for q in row.args["questions"]]


def _ids(row: Any) -> list[str]:
    return [q["id"] for q in row.args["questions"]]


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


async def _queued(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, note_id: str
) -> int:
    """How many re-ingests this note has been queued for. The batch exists to make this
    ONE per reply however many questions the reply answered."""
    async with scoped_session(maker, owner) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = 'ingest_note'"
                    " AND payload->>'note_id' = :n"
                ),
                {"n": note_id},
            )
        ).scalar_one()


async def _open_set(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, session_id: str, *asks: str
) -> tuple[str, list[str]]:
    """A conversation waiting on `asks`, and the ids the handler gave them — the setup
    every pairing test below starts from."""
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](_ask(*asks), _ctx(owner, session_id))
    (row,) = await _ledger(maker, owner, session_id)
    return session_id, _ids(row)


async def _blocks(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, note_id: str
) -> list[tuple[str, str]]:
    rows = await SqlNotesRepo(maker).list_clarifications(owner, note_id)
    assert rows is not None
    return [(c.question, c.answer) for c in rows]


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

    out = await handler(_ask(QUESTION), _ctx(owner, session_id))

    assert isinstance(out, ToolOutput)
    assert out.halt == AWAITING_OWNER
    state, _ = await _state(maker, owner, session_id)
    assert state == "waiting_on_owner"
    (row,) = await _ledger(maker, owner, session_id)
    assert row.name == ASK_OWNER_TOOL
    assert _asked(row) == [QUESTION]
    assert row.ok is True
    # It wrote no graph, and says so rather than defaulting to silence.
    assert row.domains == []
    assert row.entity_ids == []


async def test_a_second_ask_is_refused_while_the_first_set_is_open(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The refusal is now at the level the latch works at: one open SET. One reply
    consumes one set, so a second set opened behind the first would be answered by
    nothing — and the refusal has to say how many are outstanding, because "one open
    question at a time" stopped being true of a batched ask."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    handler = build_ask_owner_handlers(maker)[ASK_OWNER_TOOL]
    await handler(_ask(QUESTION, COACH), _ctx(owner, session_id))

    second = await handler(_ask(DOSE), _ctx(owner, session_id))

    assert not isinstance(second, ToolOutput)  # no halt: the turn was not ended again
    assert "already waiting" in second
    assert "2 questions" in second
    assert QUESTION in second
    assert len(await _ledger(maker, owner, session_id)) == 1


async def test_a_blank_question_records_nothing_and_does_not_stop_the_turn(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    handler = build_ask_owner_handlers(maker)[ASK_OWNER_TOOL]

    out = await handler(_ask("   "), _ctx(owner, session_id))

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

    assert "only inside a note's conversation" in await handler(_ask(QUESTION), _ctx(owner, None))
    assert "only inside a note's conversation" in await handler(
        _ask(QUESTION), _ctx(owner, orphan.id)
    )


async def test_a_waiting_conversation_cannot_be_settled(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """Constraint 6, at the only place that can enforce it. `settled` is what the
    whole-note settle sweep fires on, and it means "this pass wrote everything it meant
    to" — a pass that stopped to ask a question has not."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](_ask(QUESTION), _ctx(owner, session_id))

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
                (ToolCall("c1", ASK_OWNER_TOOL, _ask(QUESTION)),),
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
    assert _asked(calls[0]) == [QUESTION]
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
                (ToolCall("c1", ASK_OWNER_TOOL, _ask(QUESTION)),),
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
    assert [_asked(c) for c in calls] == [[QUESTION]]


# --- the owner answers --------------------------------------------------------


async def test_the_reply_becomes_a_dated_block_on_the_note_and_re_ingests_it(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """D6 + D7 in one chain: the answer is appended to the note as a timestamped block,
    the note's text now carries it, and `ingest_note` is queued so the block becomes
    chunks of the same note and the graph re-derives from notes alone."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](_ask(QUESTION), _ctx(owner, session_id))

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
    assert reply.answered == [(QUESTION, "My sister.")]
    assert reply.unanswered == []
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
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](_ask(QUESTION), _ctx(owner, session_id))
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
    """The CLAIM is the latch, and it happens BEFORE the append: the second message finds
    a thread that is no longer waiting, so the note cannot collect the same answer twice
    as source text — which, with no per-block eraser in the PWA, is the one of the two
    failure directions the owner could not undo.

    Sequential, and that word is load-bearing. This case passed against the old
    `set_state` latch too, which is exactly why it could not see that the latch was not
    one: `_ALLOWED_SOURCES["running"]` admits `running`, so a CONCURRENT second reply
    updated a second time and filed a second block.
    `test_two_overlapping_replies_and_exactly_one_claims_the_set` is the test that
    discriminates, and `claim_waiting`'s conditional UPDATE is what makes both pass."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](_ask(QUESTION), _ctx(owner, session_id))
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
    since nobody has answered yet.

    The turn's STRUCTURED answers go with it, for the same reason: on such a turn the
    whole payload is the server's, not Jeff's."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION
    )
    notes = SqlNotesRepo(maker)

    outcome = await record_owner_reply(
        maker,
        notes,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="Enacted 1 of 1 — 1 approved, 0 held.",
        answers=[(ids[0], "Enacted 1 of 1.")],
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

    assert answer is not None and answer.clarified is True
    assert answer.answered == [(QUESTION, "My sister.")]
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
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](_ask(QUESTION), _ctx(owner, session_id))
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
    await handler(_ask(QUESTION), _ctx(owner, session_id))
    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )
    await handler(_ask("Which running club?"), _ctx(owner, session_id))

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
    assert [_asked(c) for c in await _ledger(maker, owner, session_id)] == [
        [QUESTION],
        ["Which running club?"],
    ]


async def test_a_reply_turn_that_died_releases_the_note(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """A Stop, a dropped connection or a mid-turn error still has to release the live
    slot — the close runs in the turn's `finally` for exactly this."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](_ask(QUESTION), _ctx(owner, session_id))
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


# --- the batch: one ask, several questions, one reply -------------------------


async def test_one_ask_records_the_whole_set_each_question_with_its_own_id(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """R1c's core: a pass stuck on three things asks once, and the ledger row carries all
    three with an id apiece.

    The ids have to be IN THE ARGS. A reopened thread replays the ask step's `args`
    straight out of the transcript (§3b I9), so that blob is the only thing that persists
    them — an id kept anywhere else would leave a reopened thread's answers unpairable."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)

    out = await build_ask_owner_handlers(maker)[ASK_OWNER_TOOL](
        {
            "questions": [
                {
                    "question": QUESTION,
                    "blocks": "who the note's “with Sarah” resolves to",
                    "candidates": "Sarah Whitfield (sister), Sarah Chen (club)",
                },
                {"question": COACH},
                {"question": DOSE},
            ]
        },
        _ctx(owner, session_id),
    )

    assert isinstance(out, ToolOutput)
    assert out.halt == AWAITING_OWNER
    assert "3 questions" in str(out)
    (row,) = await _ledger(maker, owner, session_id)
    assert _asked(row) == [QUESTION, COACH, DOSE]
    ids = _ids(row)
    assert len(set(ids)) == 3 and all(ids)
    # What lets Jeff answer with one tap rides with the question it belongs to.
    assert row.args["questions"][0]["candidates"].startswith("Sarah Whitfield")
    assert row.args["questions"][0]["blocks"].startswith("who the note")
    state, _ = await _state(maker, owner, session_id)
    assert state == "waiting_on_owner"


async def test_a_full_structured_reply_files_every_block_and_re_ingests_once(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The arithmetic the batch was built for. Three answers are ONE turn, three blocks
    and ONE re-ingest — where three separate asks cost three passes, three re-ingests and
    three trips to the inbox (O9). `append_clarifications` is what makes the count one:
    the `ingest_state` flip and the enqueue ride inside its single transaction, so a
    caller looping over the one-pair wrapper would queue three of them."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH, DOSE
    )

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="",
        answers=[(ids[0], "My sister."), (ids[1], "Her own."), (ids[2], "25 mg.")],
    )

    assert reply is not None and reply.clarified is True
    assert reply.answered == [(QUESTION, "My sister."), (COACH, "Her own."), (DOSE, "25 mg.")]
    assert reply.unanswered == []
    assert await _blocks(maker, owner, note_id) == reply.answered
    assert await _queued(maker, owner, note_id) == 1


async def test_an_answer_naming_a_question_that_is_not_open_files_nothing(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """A settled thread reopened months later renders its question block from the
    transcript, so a stale block can post an id from a set that closed long ago. Filing it
    against whatever is open NOW would put the owner's old answer under a question nobody
    asked — a wrong sentence in their own note. It is dropped, and its question stays
    open."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH, DOSE
    )

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="",
        answers=[(ids[0], "My sister."), ("q0badf00d", "The canal loop.")],
    )

    assert reply is not None
    assert reply.answered == [(QUESTION, "My sister.")]
    assert reply.unanswered == [COACH, DOSE]
    assert await _blocks(maker, owner, note_id) == [(QUESTION, "My sister.")]
    note = await SqlNotesRepo(maker).get_note(owner, note_id)
    assert note is not None and "canal loop" not in note.body


async def test_the_composite_send_end_to_end(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """THE SEND R3f's acceptance walk is: two answers tapped, one sentence typed.

    It is the send §3b I7 designs and the omnibox invites ("answer above, or just
    reply"), and R3f's review proved the thread displayed the exact inverse of it. This
    asserts the whole outcome in one place — what the TURN says, what the NOTE gets, what
    reaches no note, and what the agent is told — because the failure was that those four
    disagreed: the turn carried only the aside, the note carried only the answers, and the
    frozen block (which reads its answers back out of the turn's text) therefore drew
    "answered" with no answers and the tapped candidate not picked."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH, DOSE
    )

    answers = [(ids[0], "My sister."), (ids[1], "Her own.")]
    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="also the dinner is cancelled",
        answers=answers,
    )
    assert reply is not None

    # 1. THE NOTE gets the two tapped answers, each under the question it answers, and
    #    nothing else. The typed sentence is not filed against DOSE.
    assert reply.answered == [(QUESTION, "My sister."), (COACH, "Her own.")]
    assert await _blocks(maker, owner, note_id) == reply.answered
    note = await SqlNotesRepo(maker).get_note(owner, note_id)
    assert note is not None and "dinner is cancelled" not in note.body

    # 2. THE THIRD QUESTION is still open — not silently spent on a sentence that does
    #    not answer it.
    assert reply.unanswered == [DOSE]

    # 3. THE TURN says both halves, pairs first, in the order the owner did them. This
    #    is the string the transcript persists and the frozen block reads back, and it is
    #    byte-identical to `asked.ownerTurnText`'s optimistic bubble.
    assert owner_turn_text("also the dinner is cancelled", reply, answers) == (
        f"Q: {QUESTION}\nA: My sister.\n\nQ: {COACH}\nA: Her own.\n\nalso the dinner is cancelled"
    )

    # 4. THE AGENT is told the sentence reached no note, and that DOSE is still open —
    #    and the turn holds no `assert_fact` off the back of it.
    assert reply.dropped == ["also the dinner is cancelled"]
    assert owner_words_reached_note(reply) is False
    notice = owner_reply_notice(reply)
    assert "dinner is cancelled" in notice and "did NOT reach the note" in notice
    assert DOSE in notice and "still open" in notice


async def test_free_prose_alone_answers_the_oldest_open_question(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The degrade that lets the batched ask merge ahead of the PWA block that fills the
    structured answers (R3f). Until it ships, every reply is prose — and prose against a
    three-question set is exactly today's semantics on a one-item set: it answers the
    oldest, never mispairs, and says so by leaving the rest in `unanswered`."""
    note_id = await _note(maker, owner)
    session_id, _ = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH, DOSE
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
    assert reply.answered == [(QUESTION, "My sister.")]
    assert reply.unanswered == [COACH, DOSE]
    assert await _blocks(maker, owner, note_id) == [(QUESTION, "My sister.")]


async def test_free_prose_beside_a_partial_set_is_not_filed_as_an_answer(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """R3f's review, finding 5 — the rule R1c wrote when the composer was the only
    affordance, reversed now that §3b I7 puts the block beside it and invites the free
    reply.

    The prose used to answer the oldest question the taps left open. On a three-question
    set that means the owner taps two, types "this note is about Kaiya not me", and that
    sentence is appended to his own note as the answer to a question it does not answer —
    permanently, searchably, with the clarification eraser as the only undo. A block that
    pairs an answer with the wrong question is a wrong sentence in the owner's corpus, so
    the typed half is no longer paired at all: it rides the turn's text for the agent to
    read, and `dropped` says it reached no note."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH, DOSE
    )

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="this note is about Kaiya not me",
        answers=[(ids[0], "My sister.")],
    )

    assert reply is not None
    assert reply.answered == [(QUESTION, "My sister.")]
    # The two the taps did not answer are still OPEN — neither was silently spent on a
    # sentence that does not answer it.
    assert reply.unanswered == [COACH, DOSE]
    assert await _blocks(maker, owner, note_id) == [(QUESTION, "My sister.")]
    assert reply.dropped == ["this note is about Kaiya not me"]
    assert owner_words_reached_note(reply) is False
    # And the agent hears both halves: the sentence that reached no note, and the
    # questions still open.
    notice = owner_reply_notice(reply)
    assert "Kaiya" in notice and "did NOT reach the note" in notice
    assert COACH in notice and "still open" in notice


async def test_free_prose_beside_a_complete_set_is_chat_and_files_no_block(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """`note_clarifications.question` is NOT NULL and non-blank in Postgres, so there is
    no shape for an unprompted block — and inventing a question the agent never asked to
    hold the owner's aside would put a sentence into their own note that nobody said. It
    stays chat, which the agent reads either way."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH
    )

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="great run by the way",
        answers=[(ids[0], "My sister."), (ids[1], "Her own.")],
    )

    assert reply is not None
    assert reply.answered == [(QUESTION, "My sister."), (COACH, "Her own.")]
    assert reply.unanswered == []
    assert await _blocks(maker, owner, note_id) == reply.answered
    note = await SqlNotesRepo(maker).get_note(owner, note_id)
    assert note is not None and "great run" not in note.body
    # ⟲ And the drop is REPORTED, which is R3's second review, finding 2. This is the
    # DESIGNED send on a `waiting_on_owner` thread, so the state said "the owner is
    # answering" while a sentence of his reached no note at all — and the reply turn's
    # `assert_fact` was bound on that state. It is bound on this instead.
    assert reply.dropped == ["great run by the way"]
    assert owner_words_reached_note(reply) is False


async def test_one_reply_consumes_the_whole_set_and_a_second_files_nothing(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The soundness claim the batch rests on, in its SEQUENTIAL case.

    The claim stays at the level a reply arrives at: ONE reply consumes the whole set, so
    a second reply finds the thread `running` and files nothing, and two replies can never
    answer the same question twice. That is what makes O11 (ii) cost no per-question
    claim — what a partial send leaves behind is not durable state, it is a sentence
    handed to the agent.

    This test awaits the first reply before sending the second, so it pins that and only
    that. The case the property actually has to survive is two replies IN FLIGHT, and it
    took a conditional UPDATE rather than the state flip to hold there — see
    `test_two_overlapping_replies_and_exactly_one_claims_the_set`."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH, DOSE
    )
    notes = SqlNotesRepo(maker)

    first = await record_owner_reply(
        maker,
        notes,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="",
        answers=[(ids[0], "My sister.")],
    )
    second = await record_owner_reply(
        maker,
        notes,
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="and her own coach",
        answers=[(ids[1], "Her own.")],
    )

    assert first is not None and first.unanswered == [COACH, DOSE]
    assert second is None
    assert await _blocks(maker, owner, note_id) == [(QUESTION, "My sister.")]
    assert await _queued(maker, owner, note_id) == 1


async def test_a_question_asked_before_the_batch_shipped_still_pairs(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The deploy-window fallback. The box is LIVE: a thread can be sitting in
    `waiting_on_owner` with a pre-R1c ledger row — `args = {"question": "..."}`, a bare
    string with no id — the moment this ships. Without the fallback that owner's answer
    pairs with nothing and their question is silently lost, which is the one thing this
    channel exists to prevent. It can go once no live thread predates the batch."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    async with scoped_session(maker, owner) as s:
        repo = NoteConversationRepo()
        await repo.record_tool_call(
            s, session_id, name=ASK_OWNER_TOOL, args={"question": QUESTION}, ok=True, domains=()
        )
        await repo.set_state(s, session_id, "waiting_on_owner")

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )

    assert reply is not None and reply.clarified is True
    assert reply.answered == [(QUESTION, "My sister.")]
    assert reply.unanswered == []


async def test_the_open_set_is_the_newest_ask_even_when_it_parses_empty(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The invariant `open_questions` states, now implemented: the newest succeeded
    `ask_owner` IS the open set, whatever it parses to.

    Falling through an empty newest row to an older one reads it backwards. The older set
    is one the owner already ANSWERED, so a reply that fell back to it would pair this
    turn's words with a question that closed weeks ago and append the pair to the owner's
    own note as source text — the mispairing `_pair` exists to prevent, arriving through
    the reader instead of the pairer. `_fit` is what keeps an empty parse out of the
    ledger; this pins what happens if one ever gets there anyway."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    repo = NoteConversationRepo()
    async with scoped_session(maker, owner) as s:
        await repo.record_tool_call(
            s,
            session_id,
            name=ASK_OWNER_TOOL,
            args={"questions": [{"id": "q1", "question": QUESTION}]},
            ok=True,
            domains=(),
        )
        await repo.record_tool_call(
            s, session_id, name=ASK_OWNER_TOOL, args={"questions": []}, ok=True, domains=()
        )
        await repo.set_state(s, session_id, "waiting_on_owner")

    async with scoped_session(maker, owner) as s:
        assert await open_questions(s, repo, session_id) == []

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="My sister.",
    )

    assert reply is not None and reply.clarified is False
    assert await _blocks(maker, owner, note_id) == []


async def test_the_inbox_and_the_reply_path_read_the_same_open_set(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """One thread, one open set, two readers — the notes tab (D4/D5) and the reply path.

    The inbox's subselect took the newest `ask_owner` row unconditionally where
    `open_questions` filters on `t.ok`, so a failed row could make the tab show one set
    while the reply consumed another: the owner answers the question he was SHOWN and his
    words are filed against a different one."""
    note_id = await _note(maker, owner)
    session_id = await _conversation(maker, owner, note_id)
    repo = NoteConversationRepo()
    async with scoped_session(maker, owner) as s:
        await repo.record_tool_call(
            s,
            session_id,
            name=ASK_OWNER_TOOL,
            args={"questions": [{"id": "q1", "question": QUESTION}]},
            ok=True,
            domains=(),
        )
        await repo.record_tool_call(
            s,
            session_id,
            name=ASK_OWNER_TOOL,
            args={"questions": [{"id": "q9", "question": DOSE}]},
            ok=False,
            domains=(),
        )
        await repo.set_state(s, session_id, "waiting_on_owner")

    async with scoped_session(maker, owner) as s:
        open_set = await open_questions(s, repo, session_id)
        (entry,) = [e for e in await repo.notes_inbox(s) if e.session_id == session_id]

    assert [q.question for q in open_set] == [QUESTION]
    assert entry.questions == [QUESTION]


async def test_two_overlapping_replies_and_exactly_one_claims_the_set(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, monkeypatch: Any
) -> None:
    """The latch, tested where it actually has to hold: two replies IN FLIGHT AT ONCE.

    `scoped_session` is READ COMMITTED and `/chat` takes no per-session lock, so the
    owner double-tapping send, the PWA retrying a dropped stream, or two devices give two
    overlapping transactions on one thread. Both read `waiting_on_owner`. The old claim
    was `set_state(..., "running")`, whose conditional UPDATE filters on
    `_ALLOWED_SOURCES["running"]` — which contains `running` — so the loser matched the
    winner's committed row and BOTH proceeded: two clarification blocks for one question,
    two re-ingests, and `note_body_sha` re-stamped off a stale read. Duplicated source
    text in the owner's own note is the one failure this module calls unrecoverable.

    The barrier is what makes the overlap real rather than likely: neither reply may
    claim until both have opened their transaction and read the row. A sequential pair
    cannot see this bug at all."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH
    )
    barrier = asyncio.Barrier(2)
    claim = NoteConversationRepo.claim_waiting

    async def gated(self: Any, session: AsyncSession, sid: str) -> bool:
        await barrier.wait()
        return await claim(self, session, sid)

    monkeypatch.setattr(NoteConversationRepo, "claim_waiting", gated)
    notes = SqlNotesRepo(maker)

    async def reply(answer: tuple[str, str]) -> Any:
        return await record_owner_reply(
            maker,
            notes,
            owner,
            session_id=session_id,
            agent=NOTE_CONVERSE_AGENT,
            message="",
            answers=[answer],
        )

    results = await asyncio.gather(reply((ids[0], "My sister.")), reply((ids[1], "Her own.")))

    assert sum(r is not None for r in results) == 1
    assert len(await _blocks(maker, owner, note_id)) == 1
    assert await _queued(maker, owner, note_id) == 1
    assert (await _state(maker, owner, session_id))[0] == "running"


async def test_two_answers_for_one_question_keep_the_last_and_report_the_first(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """R3's third review, finding 4 — the same class as the round-two blocking finding,
    in miniature.

    `_pair` accumulates into a dict, so a second answer carrying an id already filled
    OVERWRITES the first and the first goes nowhere. Last-writer-wins is the right rule
    (a re-send is a correction) and the silence was not: `dropped` came back empty, so
    `owner_words_reached_note` said every word Jeff typed had become note text and the
    turn kept `assert_fact` — while one of his own answers had reached no note."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION, COACH
    )

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="",
        answers=[(ids[0], "Dana W"), (ids[0], "Dana Whitfield"), (ids[1], "the cafe")],
    )

    assert reply is not None and reply.clarified is True
    # The note has the LAST answer, which is the rule; what changed is that the one it
    # replaced is now accounted for.
    assert await _blocks(maker, owner, note_id) == [
        (QUESTION, "Dana Whitfield"),
        (COACH, "the cafe"),
    ]
    assert reply.dropped == ["Dana W"]
    assert owner_words_reached_note(reply) is False


async def test_a_structured_answer_past_the_cap_files_nothing(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """`MAX_ANSWERS` truncates rather than 422s (a client bug degrades this turn, never
    fails it) — and truncating means the tail does NOT become blocks. Sent as the 20th
    item of a 20-item list, the real answer is dropped and its question stays open, which
    is what `OwnerReply.unanswered` then tells the agent.

    AND IT IS IN `dropped` (R3's third review, finding 4). The cut happens before `_pair`
    ever sees the list, so a truncated answer could never appear in the accounting that
    function keeps — `owner_words_reached_note` read an empty `dropped`, said everything
    landed, and left the turn holding `assert_fact` while one of Jeff's own sentences had
    reached no note at all. Unreachable today only because `ask_owner.tool` caps
    `questions` at 5 while `MAX_ANSWERS` is 10, which is two constants in two modules
    agreeing by luck, not a guard."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION
    )
    padding = [(f"stale{i}", "not an answer") for i in range(MAX_ANSWERS * 2 - 1)]

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="",
        answers=[*padding, (ids[0], "My sister.")],
    )

    assert reply is not None and reply.clarified is False
    assert reply.unanswered == [QUESTION]
    assert await _blocks(maker, owner, note_id) == []
    assert "My sister." in reply.dropped, "the truncated answer was lost without a trace"
    assert owner_words_reached_note(reply) is False


async def test_a_cut_answer_is_a_reply_even_when_nothing_inside_the_cap_survived(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """R3's fourth review, finding 4 — the ONE return the cut did not ride.

    `capped_answers` drops blanks as well as truncating, so ten blank answers ahead of a
    real one leave `structured` empty; with an empty `message` beside them the turn fell
    out of `record_owner_reply` as `None` before any of the accounting ran. The owner's
    answer was discarded, the thread stayed `waiting_on_owner`, and `owner_reply_notice`
    was handed nothing to say — the exact silence `dropped` exists to end. The write side
    was never at risk (`owner_words_reached_note(None)` is already False); the NOTICE was,
    and on a turn that lost the owner's words the notice is the whole deliverable.

    Reachable only by `MAX_ANSWERS` blank answers ahead of a real one — which is to say
    only by the same coincidence between two constants in two modules that
    `answers_over_cap`'s own docstring refuses to rely on."""
    note_id = await _note(maker, owner)
    session_id, ids = await _open_set(
        maker, owner, await _conversation(maker, owner, note_id), QUESTION
    )
    blanks = [(f"blank{i}", "   ") for i in range(MAX_ANSWERS)]

    reply = await record_owner_reply(
        maker,
        SqlNotesRepo(maker),
        owner,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        message="",
        answers=[*blanks, (ids[0], "My sister.")],
    )

    assert reply is not None, "the owner answered and the turn reported nothing at all"
    assert reply.dropped == ["My sister."]
    assert reply.clarified is False
    assert await _blocks(maker, owner, note_id) == []
    assert owner_words_reached_note(reply) is False
    # And the agent is TOLD, which is the half that was missing.
    assert "Nothing Jeff said on this turn reached the note" in owner_reply_notice(reply)
    assert reply.unanswered == [QUESTION]
