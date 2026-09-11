"""The note conversation's end-of-pass settle: the sweep it runs off its closing
reading, the tail, the stamp, and the four ways it refuses to sweep.

S1 (`docs/plans/SETTLE_OWNERSHIP.md`) scoped the whole-note sweep to the producer that
stamped each row, which stopped `integrate_note` retracting the conversation's facts. S2
paid the debt that left: projection and reprojection — `reproject_canonical_name`, the
corroboration promotion, `project_appointments` / `project_emr` /
`project_place_geofences`, `reconcile_device_bindings` — live in
`AnalysisPipeline.settle_tail` and in no other write path, and the conversation's write
path is `commit_facts` and nothing else, so a conversation-written appointment landed in
NO projection: the graph held the fact and the calendar did not.

S3 built this producer a sweep over its WRITE LEDGER, and that was removed. The argument
that removed it is not repealed and this file still pins it:

- a release is justified only when a producer RE-DERIVED the note and dropped X;
- a ledger records what a pass WROTE, so a pass that read the note and wrote nothing is
  indistinguishable from one that never looked;
- the agent holds `find_entity`/`read_entity`, is told to read before it writes and is
  rewarded for not restating what is already there, so a SILENT pass is the designed
  output rather than a statement that the note stopped saying something.

What R3 adds is not a ledger sweep by another name. It is a sweep over the pass's
CLOSING READING (`close_reading`): the model restates the whole note, every restated
identity key comes back `ALREADY` carrying the same `fact_id`, and `Reading.fact_ids` is
therefore the complete current reading S3 named as its own door. So the two halves this
file asserts are one distinction, and it is the wave:

- a pass that closed NO reading retracts nothing, whatever its ledger says;
- a pass that closed one retracts what the reading no longer names — including when the
  reading names nothing at all, which is the model saying the note says nothing.

And four refusals, each of which lands on the pre-R3 behaviour rather than on data loss:
a pass that did not end cleanly, a CLAMPED reading (a prefix of the note — sweeping it
retracts the tail), a THIRD-PARTY reading (a stranger's words may cause a fact and never
a retraction), and a pass with no reading at all.

The LLM is faked throughout (CLAUDE.md #5): the writer's router is a stub its resolver
never calls out through.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from jbrain.agent.contracts import DoneEvent, ToolCallEvent, ToolResultEvent
from jbrain.agent.graphwritetools import MAX_FACTS, NoteGraphWriter
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_accumulator import TranscriptAccumulator
from jbrain.analysis.clarify import (
    NOTE_CONVERSE_AGENT,
    PassReading,
    record_turn_writes,
    settle_conversation,
)
from jbrain.analysis.converse import pass_reading
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.settle_owner import CONVERSATION
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.note_conversation import SETTLED, NoteConversationRepo, note_body_sha
from jbrain.notes.service import NoteInfo
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

# Only the note's id and DOMAIN are read off `NoteInfo` by `converse.pass_reading`; the
# capture instant below is filler, and the writer reads the real one off the row.
CAPTURED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
async def owner(maker) -> SessionContext:  # noqa: F811
    """A REAL owner principal: `agent_sessions.principal_id` is a foreign key, so the
    shared random-uuid context the note fixtures write under will not do here."""
    return await owner_ctx(maker)


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    """The settle's pipeline. It makes no model call — `sweep_note` and `settle_tail` are
    deterministic SQL — so the router exists only to satisfy the constructor.

    Built with NO settings store, which makes one part of the tail thinner here than in
    production: `_promote_corroborated` returns early without one, so nothing below
    covers the corroboration promotion. Everything else in `settle_tail` runs."""
    return AnalysisPipeline(maker, LlmRouter({"xai": FakeLlmClient()}, {}))


def _note_info(note_id: str) -> NoteInfo:
    """The note as `converse._run_turn` holds it when it builds the settle's input. Only
    the id and the domain are read by `pass_reading`."""
    return NoteInfo(
        id=note_id,
        client_id="c1",
        domain="general",
        destination=None,
        body=APPOINTMENT_BODY,
        created_at=CAPTURED_AT,
    )


async def _settle(
    maker,  # noqa: F811
    owner: SessionContext,
    session_id: str,
    *,
    state: str = SETTLED,
    reading: PassReading | None = None,
) -> bool:
    """`settle_conversation` as both callers invoke it. `reading=None` is the reply
    path's call and the unattended pass's when it closed none."""
    return await settle_conversation(
        maker, owner, _pipeline(maker), session_id=session_id, state=state, reading=reading
    )


def _reading(writer: NoteGraphWriter, note_id: str) -> PassReading | None:
    """The pass's reading, through the PRODUCTION mapping — `converse.pass_reading`, the
    same call `_run_turn` makes — rather than a hand-built `PassReading`. A gate asserted
    against a shape only the test builds is a gate nothing holds."""
    return pass_reading(writer, _note_info(note_id))


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
    names: list[str] | None = None,
) -> None:
    """Put the writer's OWN result chips on the conversation's ledger, through the real
    accumulator and the real recorder.

    Not hand-built step dicts: `clarify.ledger_rows` reads `ToolOutcome.entities` and
    `ToolOutput.facts` off the accumulator's fold, and a ledger built from a shape
    nothing produces would pin the fold against itself. What the recorder stores here is
    what a live turn would have stored — and what the settle reads back from it is the
    TAIL's reprojection set, never the sweep's `touched`."""
    acc = TranscriptAccumulator()
    for i, out in enumerate(outs):
        name = names[i] if names is not None else "close_reading"
        acc.feed(ToolCallEvent(id=f"c{i}", name=name, arguments={}))
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
) -> tuple[str, uuid.UUID, NoteGraphWriter, list[ToolOutput]]:
    """A note whose CONVERSATION books an appointment — the real tool path, no
    hand-written rows: `resolve_entity` mints the appointment entity and `close_reading`
    states the dated `scheduledTime` the projection keys on.

    `close_reading` and not `assert_fact`, because since R3 that is the unattended pass's
    only fact verb (`agents.NOTE_INGEST_UNATTENDED_TOOLS`) — and because the reading it
    leaves on the writer is what the settle then acts on."""
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
    read = await writer.close_reading(
        {
            "title": "Dentist appointment",
            "tags": ["dentist"],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "scheduledTime",
                    "object": "2027-03-04T13:00:00",
                    "statement": f"{surface} is scheduled for 2027-03-04 at 13:00.",
                    "when": "2027-03-04T13:00:00",
                    "quote": "Dentist appointment on 2027-03-04 at 13:00",
                }
            ],
        },
        ctx,
    )
    assert isinstance(read, ToolOutput)
    assert len(read.facts) == 1, str(read)
    entity_id = (await _fact_row(maker, uuid.UUID(read.facts[0].fact_id))).entity_id
    return note_id, entity_id, writer, [resolved, read]


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
    note_id, entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    assert await _appointment_rows(maker, entity_id) == 0, (
        "the conversation's write path projected on its own — this test proves nothing"
    )

    session_id = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, session_id, outs, names=["resolve_entity", "close_reading"])
    assert await _settle(maker, owner, session_id, reading=_reading(writer, note_id))

    assert await _appointment_rows(maker, entity_id) == 1


async def test_the_settle_stamps_the_reading_and_never_blanks_a_title(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """Precondition 3, answered: the conversation stamps `note_analysis` now, because
    `close_reading` carries a title and tags and the row has to exist at all.

    NOT stamping is the failure this closes and it is not cosmetic: with no row
    `Note.analyzed` is false forever, the home stream's lifecycle chip sits permanently
    amber, the Analysis tab renders "nothing here yet" over a note whose graph is
    written, and the re-run button polls an `analyzed_at` that never moves — the PWA's
    only no-terminal re-analysis lever, spinning (CLAUDE.md #10).

    The second half is why `stamp_analysis` COALESCEs. A continuation call, or a pass
    clipped before it named the note, closes a reading whose title is empty — and the
    unconditional upsert this used to have would then wipe the heading a COMPLETE pass
    wrote. The analyzer's row is seeded first so "no row" cannot pass this."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.note_analysis (note_id, title, tags, extractor,"
                " prompt_version, analyzed_at, domain_code)"
                " VALUES (CAST(:n AS uuid), 'An older title', ARRAY['old'],"
                " 'xai:grok-4.3', 'v1', now(), 'general')"
            ),
            {"n": note_id},
        )

    session_id = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, session_id, outs, names=["resolve_entity", "close_reading"])
    assert await _settle(maker, owner, session_id, reading=_reading(writer, note_id))

    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(
                text(
                    "SELECT title, tags, extractor, domain_code FROM app.note_analysis"
                    " WHERE note_id = CAST(:n AS uuid)"
                ),
                {"n": note_id},
            )
        ).one()
    assert row.title == "Dentist appointment"
    assert row.tags == ["dentist"]
    assert row.extractor == "note_ingest"
    assert row.domain_code == "general"

    # A degraded pass — one whose reading named the note nothing — stamps the row and
    # leaves the title standing.
    titleless = _reading(writer, note_id)
    assert titleless is not None
    assert await _settle(maker, owner, session_id, reading=replace(titleless, title="", tags=()))
    async with scoped_session(maker, SYSTEM_CTX) as s:
        after = (
            await s.execute(
                text("SELECT title, tags FROM app.note_analysis WHERE note_id = CAST(:n AS uuid)"),
                {"n": note_id},
            )
        ).one()
    assert after.title == "Dentist appointment"
    assert after.tags == ["dentist"]


async def test_a_pass_that_asked_a_question_still_stamps_the_note(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """§2 rule 1: the ROW is written on every pass ending that READ the note,
    `waiting_on_owner` included — and the destructive half still is not.

    This is the ordinary shape of a question-asking pass, not an edge: the persona is
    told to record everything it can settle and ask LAST, so the pass that parks on a
    question has read the note and named it. Withholding the stamp from it left
    `Note.analyzed` false, a permanent amber "analyzing…" chip in the home stream, an
    Analysis tab reading "nothing here yet" over a note whose graph IS written, and a
    re-run button polling an `analyzed_at` that never moves — until the owner got round
    to answering, and forever if he never did (CLAUDE.md #10).

    Both halves, because the stamp moving out from behind the state gate must not take
    the sweep with it: the pass has NOT finished reading, so nothing may be retracted on
    it. The second reading here says the note is empty, which on a `settled` ending would
    retract the appointment outright."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text("DELETE FROM app.note_analysis WHERE note_id = CAST(:n AS uuid)"),
            {"n": note_id},
        )

    asked = await _conversation(maker, owner, note_id)
    reader = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    await reader.close_reading({"title": "Whose dentist?", "tags": ["dentist"], "facts": []}, ctx)
    # False: the settle did not run. The stamp is not a settle, and the return value
    # answers the question every caller asks.
    assert not await _settle(
        maker, owner, asked, state="waiting_on_owner", reading=_reading(reader, note_id)
    )

    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(
                text(
                    "SELECT title, tags, extractor, domain_code, analyzed_at"
                    " FROM app.note_analysis WHERE note_id = CAST(:n AS uuid)"
                ),
                {"n": note_id},
            )
        ).one_or_none()
    assert row is not None, "a note whose pass asked a question has no analysis row"
    assert row.title == "Whose dentist?"
    assert row.analyzed_at is not None

    # And nothing was retracted on a pass that has not finished reading.
    assert (await _fact_row(maker, fact_id)).status == "active"


async def test_the_conversations_settle_does_not_flip_the_note_to_integrated(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The flip is the TERMINAL BLOCK's, not the settle's, and the difference is the
    whole of why they are two calls (R3).

    `NoteConverseRunner._mark_integrated` fires on EVERY pass ending — a truncated turn,
    a park on `ask_owner`, a failed recorder — while this settle runs on a clean one
    alone. Folded together, a pass that ended on a question would either never flip (and
    `queue.backfill_pending_integration` would re-open a thread for it every five
    minutes, for as long as the owner took to answer) or would sweep on an ending that
    read only part of the note."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    session_id = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, session_id, outs, names=["resolve_entity", "close_reading"])
    assert await _settle(maker, owner, session_id, reading=_reading(writer, note_id))

    async with scoped_session(maker, SYSTEM_CTX) as s:
        state = (
            await s.execute(
                text("SELECT integration_state FROM app.notes WHERE id = CAST(:n AS uuid)"),
                {"n": note_id},
            )
        ).scalar_one()
    assert state == "pending_integration"


async def _rereads_the_rewritten_note(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
    note_id: str,
    body: str,
) -> tuple[str, NoteGraphWriter, list[ToolOutput]]:
    """The note is edited and re-ingested, and `note.ingested` opens a SECOND
    conversation over the new text — which closes a reading of its own that says
    something else entirely.

    This is the sharpest shape available: the note's composed body CHANGED, the producer
    read it again, and what it says the note says does not include the earlier fact."""
    await reingest_a_rewritten_body(maker, note_id, tmp_path, body)
    session_id = await _conversation(maker, owner, note_id)
    writer = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    surface = f"Bramwell Ashcote {uuid.uuid4().hex[:8]}"
    resolved = await writer.resolve_entity(
        {"entities": [{"surface": surface, "kind": "person"}]}, ctx
    )
    read = await writer.close_reading(
        {
            "title": "Bramwell the dentist",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "occupation",
                    "object": "dentist",
                    "statement": f"{surface} is a dentist.",
                    "when": "",
                    "quote": "is a dentist",
                }
            ],
        },
        ctx,
    )
    assert isinstance(resolved, ToolOutput) and isinstance(read, ToolOutput)
    assert len(read.facts) == 1, str(read)
    outs = [resolved, read]
    await _ledger(maker, owner, session_id, outs, names=["resolve_entity", "close_reading"])
    return session_id, writer, outs


async def test_a_second_reading_retracts_what_the_note_no_longer_says(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """R3's payoff, and the case S3 could not license.

    The note is REWRITTEN, re-ingested, and a second conversation reads the new text and
    states what it now says. The appointment is not in it. Before the reading existed
    this producer had to leave that fact `active` and citable forever — a visible stale
    row, chosen deliberately over a silent deletion — because a ledger of what a pass
    WROTE cannot distinguish "the note stopped saying it" from "this pass did not
    mention it".

    A `close_reading` can: it is a whole-note RE-DERIVATION, so the fact it does not
    name is a fact the note no longer states. The sweep releases this producer's claim on
    that row and retracts it because no other producer holds one — and the second pass's
    own write stays live, which is what says the settle ran rather than fell over."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))
    assert (await _fact_row(maker, fact_id)).status == "active"

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second, second_writer, second_outs = await _rereads_the_rewritten_note(
        maker, owner, tmp_path, note_id, "Bramwell Ashcote is a dentist. No appointment booked."
    )
    assert await _settle(maker, owner, second, reading=_reading(second_writer, note_id))

    row = await _fact_row(maker, fact_id)
    assert row.status == "retracted", "the reading no longer says it and it is still live"
    assert row.settle_owners == []
    assert (await _fact_row(maker, uuid.UUID(second_outs[1].facts[0].fact_id))).status == "active"


async def test_a_second_reading_that_restates_everything_retracts_nothing(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """THE INVARIANT THE WHOLE SWEEP RESTS ON, and until R3's third review nothing
    asserted it directly: a restated identity key comes back carrying the SAME `fact_id`,
    so the row lands in `touched` and the sweep spares it.

    Every other test in this file exercises the DIVERGENT reading — the note changed, or
    the model said less — which is the sweep doing its job. This is the ordinary case: an
    unchanged note read a second time (a re-run, a re-ingest, `analysis/rebuild.py`, the
    reconciler's backfill). If a re-assert ever minted a NEW row instead of refreshing the
    old one, `Reading.fact_ids` would name the new id, the old row would be in no reading,
    and the sweep would retract the note's entire graph on every clean second pass — the
    loudest possible failure, reachable from the quietest possible change.

    Asserted on the ID, not only on the status: two live rows at one key would leave the
    status assertion true while the invariant was already gone."""
    note_id, entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))
    assert (await _fact_row(maker, fact_id)).status == "active"

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
        surface = (
            await s.execute(
                text("SELECT canonical_name FROM app.entities WHERE id = CAST(:e AS uuid)"),
                {"e": str(entity_id)},
            )
        ).scalar_one()

    # A SECOND pass over the same, unchanged note: its own conversation, its own writer
    # (the worker is a new process, so nothing carries over but the graph), saying exactly
    # what the first one said.
    second = await _conversation(maker, owner, note_id)
    restater = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    resolved = await restater.resolve_entity(
        {"entities": [{"surface": surface, "kind": "appointment"}]}, ctx
    )
    read = await restater.close_reading(
        {
            "title": "Dentist appointment",
            "tags": ["dentist"],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "scheduledTime",
                    "object": "2027-03-04T13:00:00",
                    "statement": f"{surface} is scheduled for 2027-03-04 at 13:00.",
                    "when": "2027-03-04T13:00:00",
                    "quote": "Dentist appointment on 2027-03-04 at 13:00",
                }
            ],
        },
        ctx,
    )
    assert isinstance(resolved, ToolOutput) and isinstance(read, ToolOutput)
    assert len(read.facts) == 1, str(read)
    # The invariant itself: the same row, not a second one at the same key.
    assert uuid.UUID(read.facts[0].fact_id) == fact_id, str(read)

    await _ledger(maker, owner, second, [resolved, read], names=["resolve_entity", "close_reading"])
    assert await _settle(maker, owner, second, reading=_reading(restater, note_id))

    row = await _fact_row(maker, fact_id)
    assert row.status == "active", "a reading that restated everything retracted it anyway"
    assert CONVERSATION in row.settle_owners
    async with scoped_session(maker, SYSTEM_CTX) as s:
        live = (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.facts WHERE note_id = CAST(:n AS uuid)"
                    " AND predicate = 'scheduledTime' AND status = 'active'"
                ),
                {"n": note_id},
            )
        ).scalar_one()
    assert live == 1, "the restatement minted a second row at the same key"


async def test_an_empty_reading_is_a_claim_and_sweeps(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The one place an EMPTY set is not an absence.

    A `close_reading` call that names no fact is the model saying, after reading the
    whole note, that the note states nothing — which is exactly when the note's rows
    should go. It is the mirror of the test below, and the pair is the distinction the
    whole gate rests on: an empty READING sweeps, an absent one does not."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))
    assert (await _fact_row(maker, fact_id)).status == "active"

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second = await _conversation(maker, owner, note_id)
    empty = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    read = await empty.close_reading({"title": "Nothing on file", "tags": [], "facts": []}, ctx)
    assert isinstance(read, ToolOutput) and not read.facts, str(read)
    assert await _settle(maker, owner, second, reading=_reading(empty, note_id))

    assert (await _fact_row(maker, fact_id)).status == "retracted"


async def test_a_silent_second_pass_retracts_nothing(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The commonest shape, and the one a ledger sweep read backwards.

    A note is re-ingested for a reason that changes no text — an attachment landing,
    which `analysis/converse.py` names as ordinary — a second conversation opens, reads,
    finds nothing it wants to add, and answers in prose. It closes no reading. That is
    the agent working as designed, and a sweep handed its empty LEDGER released every
    `conversation` claim on the note and retracted every row no other producer held.

    `pass_reading` returns None for it, so the settle runs the tail alone. The gate is
    `Reading.calls`, never `fact_ids`: the difference between this test and the one above
    is the difference between saying nothing and saying the note says nothing."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second = await _conversation(maker, owner, note_id)
    silent = await _writer(maker, note_id)
    assert _reading(silent, note_id) is None
    assert await _settle(maker, owner, second, reading=_reading(silent, note_id))

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    assert row.settle_owners == [CONVERSATION]


async def test_a_clamped_reading_does_not_sweep(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """A clamped reading is a PREFIX of the note, and a sweep against a prefix retracts
    the tail.

    The clamp is the handler's own report (`_batch`), promoted from a result line to a
    safety gate, and `Reading.clamped` LATCHES so a clean second call cannot clear a
    first call's prefix. `maxItems` is not reliably compiled into llama.cpp's tool
    grammar, which is why this is measured by the handler rather than trusted from the
    schema — so the model cannot be relied on to have sent the whole note in one call.

    Driven by a REAL over-long list rather than by calling `mark_incomplete` by hand.
    Latch wiring is load-bearing — the refused-element test below exists because one of
    these paths had none — so a test that sets the flag itself pins the gate and nothing
    that feeds it. `MAX_FACTS + 1` well-formed facts is the whole setup: `_batch` takes
    the first `MAX_FACTS`, each of them lands, and the reading is still a prefix."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second = await _conversation(maker, owner, note_id)
    clipped = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    surface = f"bramwell ashcote {uuid.uuid4().hex[:8]}"
    resolved = await clipped.resolve_entity(
        {"entities": [{"surface": surface, "kind": "person"}]}, ctx
    )
    assert isinstance(resolved, ToolOutput) and resolved.entities, str(resolved)
    read = await clipped.close_reading(
        {
            "title": "Only the top",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": f"trait{i}",
                    "object": f"value {i}",
                    "statement": f"{surface} has trait {i}.",
                    "quote": "Booked it this morning",
                }
                for i in range(MAX_FACTS + 1)
            ],
        },
        ctx,
    )
    assert isinstance(read, ToolOutput)
    assert len(read.facts) == MAX_FACTS, str(read)
    reading = _reading(clipped, note_id)
    assert reading is not None and reading.clamped, "a real clamp did not latch"
    assert await _settle(maker, owner, second, reading=reading)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active", "a prefix of the note retracted its tail"
    assert row.settle_owners == [CONVERSATION]


async def test_a_refused_element_leaves_the_reading_incomplete(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """The model RESTATES a fact, the engine refuses the element, and the settle must not
    read that as "the note stopped saying it".

    `_assert_one` yields no write on six ordinary paths — no resolved subject handle (the
    one driven here, and the commonest: a fresh pass starts with an empty handle table
    and the model addresses `e1` from memory), no predicate, no object, an id-shaped
    object that resolved to nothing, a per-element raise, and a `commit_facts` that
    linked nothing. Each drops the fact out of `Reading.fact_ids` while the reading still
    claims to be the whole note.

    Before the latch this shape passed all four of the settle's refusals — `SETTLED`, a
    reading present, `clamped=False`, not third-party — and the sweep retracted a row the
    model had just restated, on a note nobody had edited. The result line said the
    element was refused; nothing carried that to the gate."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))
    assert (await _fact_row(maker, fact_id)).status == "active"

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second = await _conversation(maker, owner, note_id)
    # A second pass, and a handle table it never filled: `resolve_entity` was not called,
    # so `e1` stands for nothing on this writer.
    forgetful = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    read = await forgetful.close_reading(
        {
            "title": "Dentist appointment",
            "tags": ["dentist"],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "scheduledTime",
                    "object": "2027-03-04T13:00:00",
                    "statement": "The dentist appointment is on 2027-03-04 at 13:00.",
                    "when": "2027-03-04T13:00:00",
                    "quote": "Dentist appointment on 2027-03-04 at 13:00",
                }
            ],
        },
        ctx,
    )
    assert isinstance(read, ToolOutput)
    assert not read.facts, str(read)
    assert "no such handle" in read, str(read)
    reading = _reading(forgetful, note_id)
    assert reading is not None, "the call unioned no reading at all"
    assert reading.clamped, "a refused element left the reading claiming to be complete"
    assert await _settle(maker, owner, second, reading=reading)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active", "the model restated the fact and the settle retracted it"
    assert row.settle_owners == [CONVERSATION]


async def test_a_raise_between_calls_leaves_the_reading_incomplete(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth latch: a `close_reading` call that RAISES has read a prefix too.

    The other three latches are the engine DECLINING a fact the model stated — a clamped
    list, a spent budget, a refused element — and every one of them is inside or before
    the per-element loop. Nothing latched a call that died before it could decline
    anything: the pool refusing a connection, a `set_config` blip on the scoped session,
    a COMMIT that failed at block exit, a cancellation mid-batch. `loop.py:_dispatch`
    reports a raise to the model as a RECOVERABLE internal error ("try a different
    approach"), so the turn may simply end on it — `end_turn` -> `state_for_stop` ->
    `SETTLED`.

    Multi-call readings are the designed norm (the persona is told "a long note takes
    more than one call of 8 facts; send the next 8 rather than dropping the tail", and
    `READING_CALL_BUDGET` is 6), so the earlier call has already left `calls >= 1` and
    `clamped=False` — every one of the settle's four refusals passed. The sweep then
    fires against the prefix that call happened to carry and retracts everything the
    raising call was going to restate.

    Driven by a real raise out of `_load_note` on the SECOND call, which is where the
    review found it. Without the wrapper this test retracts the appointment."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))
    assert (await _fact_row(maker, fact_id)).status == "active"

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second = await _conversation(maker, owner, note_id)
    partial = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    surface = f"bramwell ashcote {uuid.uuid4().hex[:8]}"
    resolved = await partial.resolve_entity(
        {"entities": [{"surface": surface, "kind": "person"}]}, ctx
    )
    assert isinstance(resolved, ToolOutput) and resolved.entities, str(resolved)
    # Call 1 of a multi-call reading: it lands, and on its own it is a clean, unclamped
    # reading of one fact.
    head = await partial.close_reading(
        {
            "title": "Dentist appointment",
            "tags": ["dentist"],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "occupation",
                    "object": "dentist",
                    "statement": f"{surface} is a dentist.",
                    "when": "",
                    "quote": "Booked it this morning",
                }
            ],
        },
        ctx,
    )
    assert isinstance(head, ToolOutput) and len(head.facts) == 1, str(head)
    assert partial.reading.calls == 1 and partial.reading.clamped is False

    # Call 2 — the one carrying the appointment — dies before it can record or refuse
    # anything. `_load_note` is the first await inside the write session, so this is the
    # shape of every infrastructure failure between the session open and `Reading.union`.
    async def _boom(_session: object) -> list[object]:
        raise RuntimeError("the pool said no")

    monkeypatch.setattr(partial, "_load_note", _boom)
    with pytest.raises(RuntimeError):
        await partial.close_reading(
            {
                "title": "Dentist appointment",
                "tags": [],
                "facts": [
                    {
                        "subject": "e1",
                        "predicate": "scheduledTime",
                        "object": "2027-03-04T13:00:00",
                        "statement": "The dentist appointment is on 2027-03-04 at 13:00.",
                        "when": "2027-03-04T13:00:00",
                        "quote": "Dentist appointment on 2027-03-04 at 13:00",
                    }
                ],
            },
            ctx,
        )
    assert partial.reading.clamped is True, "a raise left the reading claiming to be complete"

    reading = _reading(partial, note_id)
    assert reading is not None and reading.clamped
    assert await _settle(maker, owner, second, reading=reading)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active", "the pass never got to restate it and the settle retracted it"
    assert row.settle_owners == [CONVERSATION]
    # Call 1's own write survives untouched, which is what says the settle RAN.
    assert (await _fact_row(maker, uuid.UUID(head.facts[0].fact_id))).status == "active"


async def test_a_third_party_reading_commits_but_never_sweeps(
    maker,  # noqa: F811
    owner: SessionContext,
    tmp_path,
) -> None:
    """A stranger's words may cause a FACT and never a RETRACTION (D10, plan §2).

    Without this clause an `untrusted_origin` body — a guided-intake submission the owner
    approved — would license the retraction of the owner's own facts simply by not
    mentioning them, which is the one path by which third-party text could delete
    anything in the system. The reading still COMMITS: what it says lands, and only the
    release is refused.

    The flag itself is mapped off `NoteTarget.provenance`, i.e. off the note ROW, where
    nothing the body says can reach it — that mapping is pinned in
    `tests/unit/test_note_converse.py`; this asserts what the settle does with it."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    first = await _conversation(maker, owner, note_id)
    await _ledger(maker, owner, first, outs, names=["resolve_entity", "close_reading"])
    await _settle(maker, owner, first, reading=_reading(writer, note_id))

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, first, SETTLED)
    second, second_writer, second_outs = await _rereads_the_rewritten_note(
        maker, owner, tmp_path, note_id, "Bramwell Ashcote is a dentist. No appointment booked."
    )
    stranger = _reading(second_writer, note_id)
    assert stranger is not None
    assert await _settle(maker, owner, second, reading=replace(stranger, third_party=True))

    assert (await _fact_row(maker, fact_id)).status == "active"
    # ...and the stranger's own reading landed, which is what makes the refusal narrow.
    assert (await _fact_row(maker, uuid.UUID(second_outs[1].facts[0].fact_id))).status == "active"


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
    """The three endings a pass does not settle on — precondition 2, and since R3 a gate
    on something destructive again.

    They collapse to ONE check on purpose: `settled` is a claim that the pass finished
    and everything it meant to write is written, and `state_for_stop` gives it to a clean
    stop alone. A truncated turn lands `failed`. A turn that asked lands
    `waiting_on_owner`. A turn whose recorder failed lands `failed` too, because both
    callers degrade the stop reason to `record_failed` when `record_reply_writes` returns
    False (`api/agent.py`) or `_record` raises (`converse._run_turn`).

    Handed a COMPLETE reading here, deliberately: the refusal has to be the state's, and
    a test that also withheld the reading would pass with the gate deleted."""
    note_id, _entity_id, writer, outs = await _books_an_appointment(maker, tmp_path)
    fact_id = uuid.UUID(outs[1].facts[0].fact_id)
    session_id = await _conversation(maker, owner, note_id)

    async with scoped_session(maker, owner) as s:
        await NoteConversationRepo().set_state(s, session_id, SETTLED)
    other = await _conversation(maker, owner, note_id)
    empty = await _writer(maker, note_id)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    await empty.close_reading({"title": "Nothing on file", "tags": [], "facts": []}, ctx)

    assert not await _settle(maker, owner, other, state=state, reading=_reading(empty, note_id)), (
        why
    )

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    assert row.settle_owners == [CONVERSATION]
