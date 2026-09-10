"""The note conversation's own end-of-pass settle — the tail it never ran, and the
claim it never released.

S1 (`docs/plans/SETTLE_OWNERSHIP.md`) scoped the whole-note sweep to the producer that
stamped each row, which stopped `integrate_note` retracting the conversation's facts.
It also left two debts, and this file is where both are paid:

- **the tail (S2).** Projection and reprojection — `reproject_canonical_name`, the
  corroboration promotion, `project_appointments` / `project_emr` /
  `project_place_geofences`, `reconcile_device_bindings` — live in
  `AnalysisPipeline.settle_tail` and in no other write path. The conversation's write
  path is `commit_facts` and nothing else, so a conversation-written appointment landed
  in NO projection: the graph held the fact and the calendar did not. It was masked
  while the analyzer's settle retracted those facts and then projected the dead rows
  away, which is exactly why S1 had to be followed immediately.
- **the release (S3).** A claim is released by a settle, and the conversation had none,
  so every row carrying a `conversation` claim — its own AND every row both producers
  assert — was retractable by no sweep at all, permanently, and the set grew with every
  co-asserted fact.

The gate is the dangerous half and gets its own three tests. `settle_conversation` fires
only from `SETTLED`, so a truncated turn, a turn still `waiting_on_owner`, and a turn
whose ledger did not record (`record_reply_writes` returning False, which degrades the
close to `record_failed`) all leave the sweep unfired with the facts intact. Firing on an
incomplete ledger retracts the owner's own writes — the bug S1 just closed, re-entered
through the front door.

The LLM is faked throughout (CLAUDE.md #5): the writer's router is a stub its resolver
never calls out through, and the analyzer half consumes a pre-built intent.
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
from jbrain.analysis.settle_owner import ANALYZER, CONVERSATION
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.note_conversation import SETTLED, NoteConversationRepo, note_body_sha
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import (  # noqa: F401
    ingest,
    make_note,
    maker,
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
    """The settle's pipeline. It makes no model call — `sweep_note` and `settle_tail`
    are deterministic SQL — so the router exists only to satisfy the constructor."""
    return AnalysisPipeline(maker, LlmRouter({"xai": FakeLlmClient()}, {}))


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
    assert await settle_conversation(
        maker, owner, _pipeline(maker), session_id=session_id, state=SETTLED
    )

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
    assert await settle_conversation(
        maker, owner, _pipeline(maker), session_id=session_id, state=SETTLED
    )

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
    assert await settle_conversation(
        maker, owner, _pipeline(maker), session_id=session_id, state=SETTLED
    )

    async with scoped_session(maker, SYSTEM_CTX) as s:
        state = (
            await s.execute(
                text("SELECT integration_state FROM app.notes WHERE id = CAST(:n AS uuid)"),
                {"n": note_id},
            )
        ).scalar_one()
    assert state == "pending_integration"


async def test_the_facts_the_conversation_still_asserts_keep_their_claim(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The settle is not a retraction of the pass that just ran. Everything on the
    ledger is in `touched`, so the release skips it and the claim stands — the invariant
    that makes running a sweep at the end of EVERY clean pass safe rather than reckless."""
    note_id, _entity_id, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    session_id = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, session_id, outs)

    await settle_conversation(maker, owner, _pipeline(maker), session_id=session_id, state=SETTLED)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    assert row.settle_owners == [CONVERSATION]


async def test_a_fact_the_note_no_longer_says_is_retracted_once_the_claim_is_released(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """S3's payoff, in the shape the corpus actually produces it.

    A note is re-ingested — an attachment lands, a clarification block is appended, the
    body is edited — and `note.ingested` opens a SECOND conversation over it
    (`converse.note_converse`; the first is `settled`, so the one-live index allows it).
    That pass reads the note as it now stands and no longer asserts what the first one
    did. Its settle releases the `conversation` claim on the rows outside ITS ledger, and
    a row nobody claims any more is retracted.

    Before S3 nothing released that claim at any point in the note's life, so the row
    stood active forever — a note that is no longer the sole source of truth for its own
    facts, and the set of such rows grew with every co-asserted claim."""
    note_id, _entity_id, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs)
    await settle_conversation(maker, owner, _pipeline(maker), session_id=first, state=SETTLED)
    assert (await _fact_row(maker, fact_id)).status == "active"

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second = await _conversation(maker, owner, note_id)
    await settle_conversation(maker, owner, _pipeline(maker), session_id=second, state=SETTLED)

    row = await _fact_row(maker, fact_id)
    assert row.status == "retracted"
    assert row.settle_owners == []


async def test_a_co_asserted_row_survives_the_conversation_letting_go(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The other half of the claim SET, from the side S1 could not exercise: the
    conversation releases and the ANALYZER still says so, therefore the row stands.

    Until now this direction was untestable, because the conversation had no settle to
    release with. It is the case the doc calls ordinary rather than exceptional — both
    producers read the same note off one `note.ingested` event, and a salient claim is
    exactly what both write down, at which point `decide()` refreshes ONE row."""
    note_id, _entity_id, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        # The analyzer JOINING the claim, exactly as `_claimed_by` writes it: the same
        # remove-then-append, so a re-run cannot duplicate its own claim.
        await s.execute(
            text(
                "UPDATE app.facts SET settle_owners ="
                " array_append(array_remove(settle_owners, 'analyzer'), 'analyzer')"
                " WHERE id = CAST(:f AS uuid)"
            ),
            {"f": str(fact_id)},
        )
    first = await _conversation(maker, owner, note_id)
    await settle_conversation(maker, owner, _pipeline(maker), session_id=first, state=SETTLED)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active", "the conversation retracted a row the analyzer asserts"
    assert row.settle_owners == [ANALYZER]


@pytest.mark.parametrize(
    ("state", "why"),
    [
        ("failed", "a truncated turn — max_steps, the budget, the wall clock"),
        ("failed", "a turn whose ledger did not record: `record_failed`"),
        ("waiting_on_owner", "a turn that stopped to ask the owner a question"),
    ],
)
async def test_the_sweep_does_not_fire_on_a_pass_that_did_not_end_cleanly(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
    state: str,
    why: str,
) -> None:
    """The three refusals, each with the facts left intact — precondition 2.

    They collapse to ONE gate on purpose, and the collapse is the argument: `settled` is
    a claim that the pass finished and everything it meant to write is written, and
    `state_for_stop` gives it to a clean stop alone. A truncated turn lands `failed`. A
    turn that asked lands `waiting_on_owner`. A turn whose recorder failed lands `failed`
    too, because both callers degrade the stop reason to `record_failed` when
    `record_reply_writes` returns False (`api/agent.py`) or `_record` raises
    (`converse._run_turn`) — so an UNRECORDED write can never be swept as a fact the note
    no longer says.

    The ledger is deliberately left EMPTY here, which is the sharpest version: under
    `settled` an empty ledger is a real statement and everything unpinned would go, so a
    gate that leaked would show as a retraction rather than as a subtle difference."""
    note_id, _entity_id, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    session_id = await _conversation(maker, owner, note_id)

    assert not await settle_conversation(
        maker, owner, _pipeline(maker), session_id=session_id, state=state
    ), why

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    assert row.settle_owners == [CONVERSATION]
