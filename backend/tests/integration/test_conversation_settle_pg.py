"""The note conversation's end-of-pass settle: the tail it runs, and the sweep it must
never grow.

S1 (`docs/plans/SETTLE_OWNERSHIP.md`) scoped the whole-note sweep to the producer that
stamped each row, which stopped `integrate_note` retracting the conversation's facts. S2
paid the debt that left: projection and reprojection — `reproject_canonical_name`, the
corroboration promotion, `project_appointments` / `project_emr` /
`project_place_geofences`, `reconcile_device_bindings` — live in
`AnalysisPipeline.settle_tail` and in no other write path, and the conversation's write
path is `commit_facts` and nothing else, so a conversation-written appointment landed in
NO projection: the graph held the fact and the calendar did not.

S3 would have given it a sweep as well. It was built, reviewed and REMOVED, and this
file pins the removal as hard as it pins the tail, because the premise that produced it
is true and is not a reason: *nothing ever releases a `conversation` claim*. What is
also true is that no evidence this producer has can license a retraction —

- a release is justified only when a producer RE-DERIVED the note and dropped X;
- within one session this one never drops anything (asserts once, revises by
  supersession, `correct_fact` supersedes and pins, a re-assert returns `ALREADY` with
  the same `fact_id`), so its ledger never shrinks;
- so a release could only ever remove OTHER sessions' claims;
- and judging those needs a complete current READING, which a ledger of WRITES is not:
  the agent holds `find_entity`/`read_entity`, is told to read before it writes and is
  rewarded for not restating what is already there, so a silent second pass is the
  DESIGNED output rather than a statement that the note stopped saying something.

A sound conversation sweep is therefore empty and a non-empty one is unsound. The built
version failed four ways, ending with the one that fired on the feature's own happy
path: the owner ANSWERS a question, the block is appended so the note's text only GROWS,
`record_owner_reply` re-stamps `note_body_sha`, and an earlier conversation's fact is
retracted.

What that costs, stated plainly because it is the trade and not an oversight: the owner
edits a note to delete a statement and a conversation-asserted fact stays `active` and
citable. Bounded by `purge.purge_note_artifacts` (note deletion and the corpus rebuild),
FK cascade, review-item retraction, `correct_fact`, and ordinary supersession. The
unbounded residue is a fact whose identity key is never re-asserted on a note never
purged or rebuilt.

The LLM is faked throughout (CLAUDE.md #5): the writer's router is a stub its resolver
never calls out through.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from jbrain.agent.contracts import DoneEvent, ToolCallEvent, ToolResultEvent
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_accumulator import TranscriptAccumulator
from jbrain.analysis.clarify import (
    NOTE_CONVERSE_AGENT,
    record_turn_writes,
    settle_conversation,
)
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.settle_owner import CONVERSATION
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.note_conversation import SETTLED, NoteConversationRepo, note_body_sha
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import (  # noqa: F401
    ingest,
    make_note,
    maker,
    reingest_a_rewritten_body,
)
from tests.integration.test_note_conversation_rls import owner_ctx
from tests.integration.test_rls import OWNER, database_url  # noqa: F401
from tests.integration.test_settle_cross_producer_pg import _fact_row, _writer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

APPOINTMENT_BODY = "Dentist appointment on 2027-03-04 at 13:00. Booked it this morning."


@pytest.fixture
async def owner(maker) -> SessionContext:  # noqa: F811
    """A REAL owner principal: `agent_sessions.principal_id` is a foreign key, so the
    shared random-uuid context the note fixtures write under will not do here."""
    return await owner_ctx(maker)


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    """The settle's pipeline. It makes no model call — `settle_tail` is deterministic
    SQL — so the router exists only to satisfy the constructor.

    Built with NO settings store, which makes one part of the tail thinner here than in
    production: `_promote_corroborated` returns early without one, so nothing below
    covers the corroboration promotion. Everything else in `settle_tail` runs."""
    return AnalysisPipeline(maker, LlmRouter({"xai": FakeLlmClient()}, {}))


async def _settle(
    maker,  # noqa: F811
    owner: SessionContext,
    session_id: str,
    *,
    state: str = SETTLED,
) -> bool:
    """`settle_conversation` as both callers invoke it."""
    return await settle_conversation(
        maker, owner, _pipeline(maker), session_id=session_id, state=state
    )


async def _conversation(maker, owner: SessionContext, note_id: str) -> str:  # noqa: F811
    """A note conversation as the worker opens one: an `agent_sessions` row dedicated to
    the note, and the `note_conversations` row that gives it meaning."""
    session = await AgentSessionRepo(maker).create(
        owner, domain_scopes=["general"], title="note", agent=NOTE_CONVERSE_AGENT
    )
    async with scoped_session(maker, owner) as s:
        body = (
            await s.execute(
                text("SELECT body FROM app.notes WHERE id = CAST(:n AS uuid)"), {"n": note_id}
            )
        ).scalar_one()
        await NoteConversationRepo().start(
            s, session_id=session.id, note_id=note_id, body_sha=note_body_sha(body)
        )
    return session.id


async def _ledger(
    maker,  # noqa: F811
    owner: SessionContext,
    session_id: str,
    outs: list[ToolOutput],
) -> None:
    """Put the writer's OWN result chips on the conversation's ledger, through the real
    accumulator and the real recorder.

    Not hand-built step dicts: `clarify.ledger_rows` reads `ToolOutcome.entities` and
    `ToolOutput.facts` off the accumulator's fold, and a ledger built from a shape
    nothing produces would pin the fold against itself. What the recorder stores here is
    what a live turn would have stored."""
    acc = TranscriptAccumulator()
    for i, out in enumerate(outs):
        acc.feed(ToolCallEvent(id=f"c{i}", name="assert_fact", arguments={}))
        acc.feed(
            ToolResultEvent(
                tool_call_id=f"c{i}",
                ok=True,
                summary="wrote it",
                entities=list(out.entities),
                facts=list(out.facts),
            )
        )
    acc.feed(DoneEvent(stop_reason="end_turn"))
    await record_turn_writes(maker, owner, session_id=session_id, tool_steps=acc.tool_steps())


async def _appointment_rows(maker, entity_id: uuid.UUID) -> int:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.appointments WHERE entity_id = CAST(:e AS uuid)"),
                {"e": str(entity_id)},
            )
        ).scalar_one()


async def _books_an_appointment(
    maker,  # noqa: F811
    tmp_path: Any,
) -> tuple[str, uuid.UUID, list[ToolOutput]]:
    """A note whose CONVERSATION books an appointment — the real tool path, no
    hand-written rows: `resolve_entity` mints the appointment entity and `assert_fact`
    gives it the dated `scheduledTime` the projection keys on."""
    note_id = await make_note(maker, domain="general", body=APPOINTMENT_BODY)
    await ingest(maker, note_id, tmp_path)
    writer = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    surface = f"dentist appointment {uuid.uuid4().hex[:8]}"
    resolved = await writer.resolve_entity(
        {"entities": [{"surface": surface, "kind": "appointment"}]}, ctx
    )
    assert isinstance(resolved, ToolOutput)
    assert resolved.entities, str(resolved)
    asserted = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "scheduledTime",
                    "object": "2027-03-04T13:00:00",
                    "statement": f"{surface} is scheduled for 2027-03-04 at 13:00.",
                    "when": "2027-03-04T13:00:00",
                    "quote": "Dentist appointment on 2027-03-04 at 13:00",
                }
            ]
        },
        ctx,
    )
    assert isinstance(asserted, ToolOutput)
    assert len(asserted.facts) == 1, str(asserted)
    entity_id = (await _fact_row(maker, uuid.UUID(asserted.facts[0].fact_id))).entity_id
    return note_id, entity_id, [resolved, asserted]


async def test_a_conversation_written_appointment_finally_projects(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """S2's payoff, and the thing that proves the tail actually runs.

    `project_appointments` is called from `settle_tail` and from `analysis/purge.py`,
    and from nowhere else in a write path. The conversation calls `commit_facts` only,
    which says in its own docstring that it does nothing whole-note — so before this the
    fact existed, was `active`, was citable, and the appointments read-model the PWA
    calendar and the ICS feed are built from had never heard of it.

    Asserted as a BEFORE and an AFTER over the same entity, because "there is a row" on
    its own would also pass if something else in the write path had projected it."""
    note_id, entity_id, outs = await _books_an_appointment(maker, tmp_path)
    assert await _appointment_rows(maker, entity_id) == 0, (
        "the conversation's write path projected on its own — this test proves nothing"
    )

    session_id = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, session_id, outs)
    assert await _settle(maker, owner, session_id)

    assert await _appointment_rows(maker, entity_id) == 1


async def test_the_conversations_settle_never_stamps_the_analysis_row(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """Precondition 3, pinned from the other side: the conversation has no title or tags
    verb, and `stamp_analysis`'s `on_conflict_do_update` sets `title` and `tags`
    unconditionally — no `WHERE`, no `COALESCE`. So a conversation wired to the WHOLE
    settle would write `title = NULL, tags = {}` over whatever `integrate_note` stamped,
    and the note's Analysis tab would lose its heading on every pass.

    The analyzer's row is seeded here rather than assumed absent, because "no row" would
    pass even if the conversation stamped an empty one over nothing."""
    note_id, _, outs = await _books_an_appointment(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.note_analysis (note_id, title, tags, extractor,"
                " prompt_version, analyzed_at, domain_code)"
                " VALUES (CAST(:n AS uuid), 'Dentist appointment', ARRAY['dentist'],"
                " 'xai:grok-4.3', 'v1', now(), 'general')"
            ),
            {"n": note_id},
        )

    session_id = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, session_id, outs)
    assert await _settle(maker, owner, session_id)

    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(
                text("SELECT title, tags FROM app.note_analysis WHERE note_id = CAST(:n AS uuid)"),
                {"n": note_id},
            )
        ).one()
    assert row.title == "Dentist appointment"
    assert row.tags == ["dentist"]


async def test_the_conversations_settle_does_not_flip_the_note_to_integrated(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """Precondition 4, the other unowned one. `integration_state = 'integrated'` is
    flipped by `integrate_note` alone and read by `queue.backfill_pending_integration`,
    the workflow dispatcher and scheduler, and `analysis/rebuild.py`. A conversation that
    can park on `ask_owner` for days cannot be what declares a note integrated — and a
    settle that flipped it would strand the corpus the moment `integrate_note` retires."""
    note_id, _, outs = await _books_an_appointment(maker, tmp_path)
    session_id = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, session_id, outs)
    assert await _settle(maker, owner, session_id)

    async with scoped_session(maker, SYSTEM_CTX) as s:
        state = (
            await s.execute(
                text("SELECT integration_state FROM app.notes WHERE id = CAST(:n AS uuid)"),
                {"n": note_id},
            )
        ).scalar_one()
    assert state == "pending_integration"


async def _rewrites_the_note(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
    note_id: str,
    body: str,
) -> tuple[str, list[ToolOutput]]:
    """The note is edited and re-ingested, and `note.ingested` opens a SECOND
    conversation over the new text — which asserts something of its own.

    This is the sharpest shape available: the note's composed body CHANGED, the producer
    read it again, and what it wrote does not include the earlier fact. If any evidence
    could license this producer retracting, it would be this — and it does not."""
    await reingest_a_rewritten_body(maker, note_id, tmp_path, body)
    session_id = await _conversation(maker, owner, note_id)
    writer = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    surface = f"Bramwell Ashcote {uuid.uuid4().hex[:8]}"
    resolved = await writer.resolve_entity(
        {"entities": [{"surface": surface, "kind": "person"}]}, ctx
    )
    asserted = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "occupation",
                    "object": "dentist",
                    "statement": f"{surface} is a dentist.",
                    "when": "",
                    "quote": "is a dentist",
                }
            ]
        },
        ctx,
    )
    assert isinstance(resolved, ToolOutput) and isinstance(asserted, ToolOutput)
    assert len(asserted.facts) == 1, str(asserted)
    outs = [resolved, asserted]
    await _ledger(maker, owner, session_id, outs)
    return session_id, outs


async def test_the_conversations_settle_retracts_nothing_ever(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The decision, pinned: this producer's settle has no sweep and cannot acquire one
    by accident.

    A sweep was built here (S3), reviewed and REMOVED, and the reason is a closed
    argument rather than a bug count. A release is justified only when a producer
    re-derived the note and dropped X. Within one session this producer never drops
    anything — it asserts once and revises by supersession, `correct_fact` supersedes and
    pins rather than retracting, and a re-assert returns `ALREADY` with the same
    `fact_id`, so its ledger never shrinks. So the only claims a release could remove are
    OTHER sessions', and judging those needs a complete current READING of the note,
    which a ledger of what a pass WROTE structurally is not: the agent holds
    `find_entity`/`read_entity`, is told to read before it writes and is rewarded for not
    restating what is there, so a silent second pass is the DESIGNED output. A sound
    conversation sweep is empty; a non-empty one is unsound.

    The case here is the one that most looks like a licence to retract and is not: the
    note is REWRITTEN, re-ingested, and a second conversation reads the new text and
    asserts something else entirely. The first conversation's fact survives, `active`,
    still claimed. That is the leak S2-only accepts, and it is deliberate — a visible
    stale row is the failure this design chose over a silent deletion.

    Its predecessor asserted the opposite, and that assertion was the bug."""
    note_id, _entity_id, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs)
    await _settle(maker, owner, first)
    assert (await _fact_row(maker, fact_id)).status == "active"

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second, second_outs = await _rewrites_the_note(
        maker, owner, tmp_path, note_id, "Bramwell Ashcote is a dentist. No appointment booked."
    )
    assert await _settle(maker, owner, second)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active", "the conversation retracted a fact it has no evidence about"
    assert row.settle_owners == [CONVERSATION]
    # ...and the second pass's own write is live, so the settle did run.
    assert (await _fact_row(maker, uuid.UUID(second_outs[1].facts[0].fact_id))).status == "active"


async def test_a_silent_second_pass_retracts_nothing(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The commonest shape, and the one a sweep read backwards.

    A note is re-ingested for a reason that changes no text — an attachment landing,
    which `analysis/converse.py` names as ordinary — a second conversation opens, reads,
    finds nothing it wants to add, and answers in prose. Its ledger is empty. That is the
    agent working as designed, and a note-scoped sweep handed that empty set released
    every `conversation` claim on the note and retracted every row no other producer
    held.

    Nothing here needs a gate or a refusal to be safe now: there is no release to gate."""
    note_id, _entity_id, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs)
    await _settle(maker, owner, first)

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second = await _conversation(maker, owner, note_id)
    assert await _settle(maker, owner, second)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    assert row.settle_owners == [CONVERSATION]


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ("failed", "a truncated turn — max_steps, the budget, the wall clock"),
        ("failed", "a turn whose ledger did not record: `record_failed`"),
        ("waiting_on_owner", "a turn that stopped to ask the owner a question"),
    ],
)
async def test_the_settle_does_not_run_on_a_pass_that_did_not_end_cleanly(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
    state: str,
    why: str,
) -> None:
    """The three endings a pass does not settle on — precondition 2.

    They collapse to ONE gate on purpose: `settled` is a claim that the pass finished and
    everything it meant to write is written, and `state_for_stop` gives it to a clean stop
    alone. A truncated turn lands `failed`. A turn that asked lands `waiting_on_owner`. A
    turn whose recorder failed lands `failed` too, because both callers degrade the stop
    reason to `record_failed` when `record_reply_writes` returns False (`api/agent.py`) or
    `_record` raises (`converse._run_turn`).

    With the sweep removed the gate protects nothing destructive — projecting is never
    destructive — and it is kept for two reasons: it is the shape the plan specifies for a
    pass end, and a caller who did add a sweep would otherwise inherit no gate at all.
    Pinned as a refusal rather than as an absence of damage, because the damage is what
    stopped being possible, not the gate."""
    note_id, _entity_id, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    session_id = await _conversation(maker, owner, note_id)

    assert not await _settle(maker, owner, session_id, state=state), why

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    assert row.settle_owners == [CONVERSATION]
