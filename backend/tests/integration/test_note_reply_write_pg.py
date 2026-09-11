"""`correct_fact` / `merge_entities` against real Postgres — the on-reply writes.

W3 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md. The LLM is faked (CLAUDE.md #5);
everything that decides how a write LANDS is real, because the design claim of both
tools is that they add no second write path. `correct_fact` is `commit_facts` with one
extra flag, and `merge_entities` stages the node op the shipped enact already runs.

What each test is defending:

- **the correction actually out-argues the graph** (D11). `ExtractedFact.correction` is
  what `supersession.decide()` reads to supersede every current head and commit the new
  value active + PINNED regardless of temporal order, and pinned is what stops a later
  note flipping it back. Asserted on the ROWS, not on the result text, because the
  result text is the model's window and the rows are the fact;
- **the identity key, not a fact id** — including the multi-row case, where the handler
  lists what is live, writes NOTHING, and refuses, because a set-valued edge's identity
  IS its object and there is no single write that changes one. That is the whole reason
  `read_entity` does not need a v5;
- **an `object` that is an entity id becomes an EDGE**, resolved under the turn's own
  scopes — never a bare uuid stored as a literal value on a pinned row;
- **the call budget counts across the conversation**, which is only true while one
  writer serves it. A fresh writer per call made `CORRECT_CALL_BUDGET` inert;
- **the reply turn really reaches `resolve_entity` and `assert_fact`** (D8's set is a
  superset), because a reply turn holding only `correct_fact` pins every fact it learns;
- **the fold is staged and never enacted** (constraint 12). A note conversation is
  domain-narrowed by construction, `merge_entity_pair` asks Postgres
  `app.is_full_owner()` before any statement, and a half-completed cross-domain fold
  leaves facts stranded on a tombstone. So: a Proposal exists, and both entities are
  still live and unrepointed;
- **a tombstoned id is followed to its survivor** (`entities.live_entity_by_id`) rather
  than folded onto again — the shape `analysis/repo.resolve_review`'s merge-accept arm
  still reaches, and the reason its shape is not copied here;
- **the turn's own read scope is the ceiling on what either tool can be pointed at.**
  The write session runs at full owner scope (constraint 2), so the gate has to be on
  the ADDRESS: an entity the conversation cannot read is an entity it cannot correct.
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from jbrain.agent.agents import (
    NOTE_INGEST_ON_REPLY_TOOLS,
    agent_for_owner_reply,
    narrow_for_unprompted_reply,
)
from jbrain.agent.contracts import DoneEvent, ToolCallEvent, ToolResultEvent
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.replytools import CORRECT_FACT, MERGE_ENTITIES, build_reply_write_handlers
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.transcript_accumulator import TranscriptAccumulator
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.analysis.clarify import NOTE_CONVERSE_AGENT, record_reply_writes
from jbrain.analysis.entities import merge_entity_pair, normalize_alias
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact
from jbrain.models.note_conversation import NoteConversationRepo, note_body_sha
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import (  # noqa: F401
    ingest,
    make_note,
    maker,
)
from tests.integration.test_note_conversation_rls import owner_ctx as _owner_ctx
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

BODY = (
    "Talked to Jeff about the move. He said he lives at 118 Pine Ave now, and that "
    "Kaiya is seen by Dr. Patel."
)


@pytest.fixture
async def owner_ctx(maker) -> SessionContext:  # noqa: F811
    """A real owner principal, as a FIXTURE.

    `test_note_conversation_rls.owner_ctx` is a plain async helper its own module calls
    directly; every other importer does the same. This module alone asks for it by
    PARAMETER, and without this wrapper pytest could not resolve the name — so all eight
    tests below errored at setup with "fixture 'owner_ctx' not found" and the entire
    `correct_fact` / `merge_entities` suite had never once executed. The same one-line
    wrapper `test_ask_owner_pg.py` uses for the same helper."""
    return await _owner_ctx(maker)


def _router() -> LlmRouter:
    return LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})


def _handlers(maker) -> dict[str, Any]:  # noqa: F811
    """The two handlers as `readtools.build_registry` wires them — the same repos the
    chat registry passes, so nothing here is a private path into the write."""
    return build_reply_write_handlers(
        maker,
        ProposalRepo(maker),
        SqlAnalysisRepo(maker),
        SqlNotesRepo(maker),
        _router(),
    )


async def _conversation(maker, owner: SessionContext, note_id: str) -> str:  # noqa: F811
    """A real note conversation: an `agent_sessions` row under the note persona plus the
    `note_conversations` row that gives it a note. Both tools find their note through
    THIS, never through an argument."""
    async with scoped_session(maker, owner) as s:
        session = await AgentSessionRepo(maker).create_on(
            s, owner, domain_scopes=["general"], title="note", agent="note_ingest"
        )
        note = await SqlNotesRepo(maker).get_note(owner, note_id)
        assert note is not None
        await NoteConversationRepo().start(
            s, session_id=session.id, note_id=note_id, body_sha=note_body_sha(note.body)
        )
    return session.id


def _ctx(
    owner: SessionContext,
    session_id: str,
    scopes: tuple[str, ...] = ("general",),
    *,
    answering: bool = True,
):
    """The turn as `/chat` builds it for a note thread: the owner NARROWED to the
    conversation's own scopes (constraint 2's `(note_domain, 'general')`), carrying this
    turn's effective allowlist.

    `agent_tools` is not decoration here. `AgentLoop` fills it with the names this turn
    may actually dispatch, and `correct_fact`'s empty-address arm reads it: `answering
    =False` is the turn `narrow_for_unprompted_reply` took `assert_fact` off, i.e. one
    whose words never reached the note."""
    narrowed = SessionContext(
        principal_id=owner.principal_id,
        principal_kind="owner",
        owner_scoped=True,
        domain_scopes=scopes,
    )
    allowed = NOTE_INGEST_ON_REPLY_TOOLS
    if not answering:
        allowed = (
            narrow_for_unprompted_reply(agent_for_owner_reply(NOTE_CONVERSE_AGENT)).tools
            or frozenset()
        )
    return ToolContext(
        session=narrowed,
        scopes=scopes,
        agent_session_id=session_id,
        agent_tools=frozenset(allowed),
    )


async def _entity(maker, name: str, *, domain: str = "general", kind: str = "Person") -> str:  # noqa: F811
    """A confirmed entity with its canonical alias, so `find_entity`-shaped name lookup
    reaches it the way the model's own addressing does."""
    eid = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                " VALUES (:id, :kind, :name, 'confirmed', :domain)"
            ),
            {"id": str(eid), "kind": kind, "name": name, "domain": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_aliases (id, entity_id, alias, alias_norm, domain_code)"
                " VALUES (:aid, :id, :a, :norm, :domain)"
            ),
            {
                "aid": str(uuid.uuid4()),
                "id": str(eid),
                "a": name,
                "norm": normalize_alias(name),
                "domain": domain,
            },
        )
    return str(eid)


async def _fact(
    maker,  # noqa: F811
    entity_id: str,
    note_id: str,
    *,
    predicate: str,
    statement: str,
    value: str | None = None,
    object_id: str | None = None,
    kind: str = "attribute",
) -> str:
    fid = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, note_id, predicate, qualifier, kind,"
                " statement, value_json, object_entity_id, assertion, status, domain_code,"
                " reported_at, temporal_precision, extractor, prompt_version)"
                " VALUES (:id, :eid, :nid, :p, '', :k, :st, CAST(:v AS jsonb), :oid,"
                " 'asserted', 'active', 'general', now(), 'unknown', 'test', 'test-v1')"
            ),
            {
                "id": str(fid),
                "eid": entity_id,
                "nid": note_id,
                "p": predicate,
                "k": kind,
                "st": statement,
                "v": json.dumps({"value": value}) if value is not None else None,
                "oid": object_id,
            },
        )
    return str(fid)


async def _fact_row(maker, fact_id: uuid.UUID) -> Any:  # noqa: F811
    """One row by id — asserted on the ROW, never on the result text, because the result
    text is the model's window and the row is the fact."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (await s.execute(select(Fact).where(Fact.id == fact_id))).scalar_one()


async def _rows(maker, entity_id: str, predicate: str) -> list[Any]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return list(
            (
                await s.execute(
                    select(Fact).where(
                        Fact.entity_id == uuid.UUID(entity_id), Fact.predicate == predicate
                    )
                )
            )
            .scalars()
            .all()
        )


# --- correct_fact -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_answering_with_a_contested_value_settles_nothing(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """O15's crux, pinned as CURRENT behaviour because it is a dead end and the owner is
    being asked to choose a fix for it (`AGENT_INGEST_REWRITE.md` §8).

    Under one channel the agent's obligation on a hold ends at "ask the owner which is
    right". The natural answer is one of the two contested values — and that answer
    changes nothing. `decide()`'s idempotency short-circuit matches the `pending_review`
    row carrying that value and returns `refresh_id` BEFORE the correction branch is
    reached, and the refresh path writes neither `status` nor `pinned`. So the row stays
    held and unpinned, nothing goes live, and the result says "ask the owner which is
    right" — to the owner who just answered.

    The correction branch's own comment is honest about the assumption that fails here:
    "An identical-value restatement was already refreshed above, so reaching here means
    a genuine override." True when the restatement is idempotent noise; false when the
    matched row is a HELD side of a live contest and the restatement is the verdict.

    In-tree precedent for the fix is eight lines up: `_lab_status_transition` is
    deliberately placed ahead of the same short-circuit "so a same-value correction still
    supersedes". Ordering alone is not sufficient though — the correction branch puts
    `pending_review` heads in `hold_ids`, so it would still leave the loser held. O15
    needs both halves, which is why it is the owner's call and not a patch.

    **When O15 is resolved this test flips**, and what it becomes is the acceptance
    check: the answered value active and pinned, the other side no longer a live head."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins")
    # The state an attribute collision leaves behind: two birthdays, neither live.
    a = await _fact(
        maker,
        jeff,
        note_id,
        predicate="birthDate",
        statement="Jeff was born 1990-03-03.",
        value="1990-03-03",
    )
    b = await _fact(
        maker,
        jeff,
        note_id,
        predicate="birthDate",
        statement="Jeff was born 1985-11-12.",
        value="1985-11-12",
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text("UPDATE app.facts SET status = 'pending_review' WHERE id IN (:a, :b)"),
            {"a": a, "b": b},
        )

    # The owner answers the question the hold result told the agent to ask.
    out = await _handlers(maker)[CORRECT_FACT](
        {
            "entity": jeff,
            "predicate": "birthDate",
            "qualifier": "",
            "object": "1985-11-12",
            "statement": "Jeff was born 1985-11-12.",
            "when": "",
            "replaces": "",
        },
        _ctx(owner_ctx, session_id),
    )

    rows = {str(f.id): f for f in await _rows(maker, jeff, "birthDate")}
    # No third row: the correction was absorbed as a refresh of the row it names.
    assert len(rows) == 2
    # And it settled NOTHING — neither value is live, and the answer is not pinned.
    assert rows[b].status == "pending_review"
    assert rows[b].pinned is False
    assert rows[a].status == "pending_review"
    assert not [f for f in rows.values() if f.status == "active"]
    # The result is honest about the outcome (F1's STILL_HELD line) and that is the
    # sharp end of it: the only advice left is to ask the person who just answered.
    body = str(out)
    assert "already recorded, and STILL NOT LIVE" in body
    assert "ask the owner which is right" in body


@pytest.mark.asyncio
async def test_a_correction_force_supersedes_the_head_and_pins_the_new_value(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """D11, on the rows. The old head is superseded (kept as history, never deleted —
    R1: retraction is not a verb), and the new one is `pinned`, which is what stops a
    later non-correction note flipping it back."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins")
    old = await _fact(
        maker,
        jeff,
        note_id,
        predicate="homeLocation",
        statement="Jeff lives at 118 Pine Ave.",
        value="118 Pine Ave",
        kind="state",
    )

    out = await _handlers(maker)[CORRECT_FACT](
        {
            "entity": jeff,
            "predicate": "homeLocation",
            "qualifier": "",
            "object": "412 Oak St",
            "statement": "Jeff lives at 412 Oak St.",
            "when": "",
            "replaces": "",
        },
        _ctx(owner_ctx, session_id),
    )
    body = str(out)
    # The result names what the SERVER did unasked — the model's only window into
    # `decide()` (TOOL_SURFACE, "Result shapes are the ACI").
    assert "412 Oak St" in body
    assert "kept as history" in body
    assert "correct_fact: 5 calls left this note" in body

    rows = {str(f.id): f for f in await _rows(maker, jeff, "homeLocation")}
    assert rows[old].status == "superseded"
    live = [f for f in rows.values() if f.status == "active"]
    assert len(live) == 1
    assert live[0].statement == "Jeff lives at 412 Oak St."
    # PINNED. The whole point of D11's flag surviving the correction-note retirement.
    assert live[0].pinned is True
    # Sourced to the conversation's OWN note — the note whose reading Jeff corrected.
    assert str(live[0].note_id) == note_id
    # And the tool reported a real fact write, so the D3 chip sees it — and, since
    # W4c/1, so does the 0191 ledger: `/chat` records the reply turn's calls at the same
    # seam the unattended pass records at. The test below is where that is asserted end
    # to end, off THIS tool's own output.
    assert isinstance(out, ToolOutput) and len(out.facts) == 1
    # D3's supersession state, with the "before" the diff needs. `ClaimDiffView` — the
    # reason the app's one diff renderer was extracted from `ClaimDiff.tsx` — renders
    # ONLY when `replaced` carries the superseded statement, so a `replaced` write that
    # dropped it would leave both the extraction and the state pointless: the owner
    # would be told a value changed and never shown what it changed from.
    write = out.facts[0]
    assert write.outcome == "replaced" and write.status == "replaced"
    assert write.replaced is not None and "118 Pine Ave" in write.replaced
    assert write.predicate == "homeLocation"
    assert write.value == "412 Oak St"
    # An owner correction rests on Jeff's own message, never on a photo.
    assert write.from_attachment is False


@pytest.mark.asyncio
async def test_a_correction_at_an_empty_address_records_and_pins_anyway(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """Jeff saying "no, it's X" when nothing is on file is still Jeff saying X. Recording
    it unpinned would leave the next note free to overwrite the thing he just told us.

    ON AN ANSWERING TURN, which is what `_ctx` builds: his words became the note's text,
    so the pinned row he just caused has source text behind it. The same arm on a turn
    where they did not is refused — the test below — and that is the whole difference.

    This is also the reason `assert_fact` has to be REACHABLE on a reply turn: pinning is
    right for something Jeff disputed and wrong for everything else, and a reply turn
    holding only this verb pins every fact it learns (see the on-reply tests below).

    The entity's name is unique to this test on purpose. `_entity` inserts a fresh row
    every call and this address is by NAME, so sharing "Jeff Hopkins" with the test above
    made the lookup ambiguous and the correction was refused — which is how a test that
    never ran also had a latent isolation bug."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    # A name of its OWN. The suite shares one database, and this is the only test that
    # addresses by NAME, so reusing "Jeff Hopkins" resolved onto the entity a sibling
    # test had already given a `homeLocation` — the test passed alone and failed in file
    # order. It could not surface before: the whole module errored at setup.
    jeff = await _entity(maker, "Jeff Hopkins of the empty address")

    await _handlers(maker)[CORRECT_FACT](
        {
            # by NAME, not id — the note's own words
            "entity": "Jeff Hopkins of the empty address",
            "predicate": "homeLocation",
            "qualifier": "",
            "object": "412 Oak St",
            "statement": "Jeff lives at 412 Oak St.",
            "when": "",
        },
        _ctx(owner_ctx, session_id),
    )
    live = [f for f in await _rows(maker, jeff, "homeLocation") if f.status == "active"]
    assert len(live) == 1
    assert live[0].pinned is True


@pytest.mark.asyncio
async def test_an_unprompted_turn_is_refused_the_empty_address_and_keeps_the_correction(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """R3's second review, finding 3 — and it is a surgical refusal, not a subtraction.

    `narrow_for_unprompted_reply` takes `assert_fact` off a turn whose words never
    reached the note, and left `correct_fact` whole. That was worse than the loss it
    prevented: at an EMPTY address `decide()`'s correction branch commits active +
    PINNED, so the agent was told it could not record a new fact while holding a verb
    that records one permanently — a row citing text that exists nowhere, which no
    re-reading of the note can falsify and no correction note can reach.

    But simply dropping the verb would take away the owner's own repair path: correcting
    a fact that IS on file is exactly what a settled thread is for, and pinning is the
    designed mechanism there. So the ARM is refused and the verb is not, and both halves
    are asserted on one turn:

    - the empty address writes NOTHING and says what to do instead;
    - the live head is still corrected, superseded and pinned, on the same turn.

    The condition is the turn's own allowlist (`ToolContext.agent_tools`), which is where
    the narrowing already lands — not a re-read of the thread state, which is `running`
    by the time a tool dispatches."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    dana = await _entity(maker, "Dana of the unprompted reply")
    on_file = await _fact(
        maker,
        dana,
        note_id,
        predicate="homeLocation",
        statement="Dana lives at 118 Pine Ave.",
        value="118 Pine Ave",
    )
    unprompted = _ctx(owner_ctx, session_id, answering=False)
    handlers = _handlers(maker)

    # (a) the empty address: a NEW pinned fact out of words the note never received.
    refused = str(
        await handlers[CORRECT_FACT](
            {
                "entity": "Dana of the unprompted reply",
                "predicate": "jobTitle",
                "qualifier": "",
                "object": "CTO",
                "statement": "Dana is the CTO.",
                "when": "",
            },
            unprompted,
        )
    )
    assert "holds nothing on file" in refused
    assert "not recorded" in refused
    assert not await _rows(maker, dana, "jobTitle"), "the refused arm wrote a row anyway"

    # (b) the same turn, correcting what IS on file: untouched, and still pinned.
    ok = await handlers[CORRECT_FACT](
        {
            "entity": "Dana of the unprompted reply",
            "predicate": "homeLocation",
            "qualifier": "",
            "object": "412 Oak St",
            "statement": "Dana lives at 412 Oak St.",
            "when": "",
        },
        unprompted,
    )
    assert "412 Oak St" in str(ok), str(ok)
    live = [f for f in await _rows(maker, dana, "homeLocation") if f.status == "active"]
    assert len(live) == 1
    assert live[0].pinned is True
    assert str(live[0].id) != on_file, "the head on file was refreshed rather than superseded"

    # (c) and on an ANSWERING turn the empty address still records, because there his
    # words are the note's text. Same handler, same address, one flag apart.
    answered = await handlers[CORRECT_FACT](
        {
            "entity": "Dana of the unprompted reply",
            "predicate": "jobTitle",
            "qualifier": "",
            "object": "CTO",
            "statement": "Dana is the CTO.",
            "when": "",
        },
        _ctx(owner_ctx, session_id),
    )
    assert "holds nothing on file" not in str(answered)
    minted = [f for f in await _rows(maker, dana, "jobTitle") if f.status == "active"]
    assert len(minted) == 1 and minted[0].pinned is True


@pytest.mark.asyncio
async def test_a_reply_turn_correction_lands_on_the_ledger_with_the_real_fact_id(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """W4c/1, off this file's own tool rather than a synthesised chip.

    The reply turn is an ordinary `/chat` turn, so until W4c/1 a `correct_fact` here
    reached `app.facts` and the D3 rung and reached `app.note_conversation_tool_calls`
    nowhere. `api/agent.py` now runs the turn's steps through the same recorder the
    unattended pass uses (`clarify.record_reply_writes`), so the ids the WRITE PATH
    reported — not the ones the model asked for — are what the settle sweep will read
    back as `touched`. Asserted on the row, and on the id matching the fact that is
    actually live, because a ledger that records a plausible-looking id it did not write
    is worse than one that records nothing: it SPARES a fact the sweep should retract."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff of the recorded correction")

    out = await _handlers(maker)[CORRECT_FACT](
        {
            "entity": "Jeff of the recorded correction",
            "predicate": "homeLocation",
            "qualifier": "",
            "object": "9 Ledger Row",
            "statement": "Jeff lives at 9 Ledger Row.",
            "when": "",
        },
        _ctx(owner_ctx, session_id),
    )
    assert isinstance(out, ToolOutput) and len(out.facts) == 1

    # The turn ends: the transcript first (the `done` path), then the ledger in the
    # `finally`, both under the same run id — the order `api/agent.py` runs them in, and
    # the reason the rows can bind to this run's assistant turn at all.
    # A REAL run row: `agent_turns.run_id` is a foreign key into `app.runs`.
    run_id = await AgentRunLog(maker).start(
        owner_ctx, session_id=session_id, prompt_version="reply-turn-test"
    )
    acc = TranscriptAccumulator()
    acc.feed(ToolCallEvent(id="k1", name=CORRECT_FACT, arguments={"predicate": "homeLocation"}))
    acc.feed(
        ToolResultEvent(
            tool_call_id="k1",
            ok=True,
            summary=str(out),
            entities=list(out.entities),
            facts=list(out.facts),
        )
    )
    acc.feed(DoneEvent(stop_reason="end_turn"))
    steps = acc.tool_steps()
    await AgentTranscript(maker).record_exchange(
        owner_ctx,
        session_id=session_id,
        run_id=run_id,
        user_text="No — 9 Ledger Row.",
        assistant_text="Corrected.",
        tools=steps,
        reasoning="",
    )
    assert await record_reply_writes(
        maker,
        owner_ctx,
        session_id=session_id,
        agent=NOTE_CONVERSE_AGENT,
        run_id=run_id,
        tool_steps=steps,
    )

    repo = NoteConversationRepo()
    async with scoped_session(maker, owner_ctx) as s:
        calls = await repo.tool_calls(s, session_id)
        writes = await repo.writes(s, session_id)
    assert [c.name for c in calls] == [CORRECT_FACT]
    row = calls[0]
    assert row.ok is True
    assert [str(f) for f in row.fact_ids] == [out.facts[0].fact_id]
    assert row.domains == ["general"]
    assert row.turn_id is not None

    live = [f for f in await _rows(maker, jeff, "homeLocation") if f.status == "active"]
    assert [str(f.id) for f in live] == [out.facts[0].fact_id]
    assert writes.facts == {uuid.UUID(out.facts[0].fact_id)}


@pytest.mark.asyncio
async def test_a_multi_row_key_is_listed_and_refused_never_half_corrected(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """The addressing design, after the affordance that could not work was removed.

    `(entity, predicate, qualifier)` does not name one fact when the predicate is
    set-valued, and it CANNOT be made to. `entity_view` yields several groups at one key
    only for a non-functional relationship (it splits per object), while `decide()`'s
    correction branch fires only on a `single_head` address — state / attribute /
    preference / FUNCTIONAL relationship. The two are exclusive, so on every key that can
    hold several rows the correction flag is a no-op, and a set-valued edge's identity is
    its object (`_facts_at_key` keeps `object_entity_id` in the key), so there is no
    single write that changes one.

    The tool used to answer with `f1`/`f2` handles and invite a retry with `replaces`.
    The retry left both originals live, added a THIRD edge, and reported `ok … replaced`;
    the disabled version of this test asserted `len(rows) == 3` without checking status,
    so it would have passed while asserting the bug. `replaces` was validated against the
    listing and then never read again — grep found four mentions, all of them the
    validation."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins (multi row)")
    civic = await _entity(maker, "the Civic", kind="Thing")
    kayak = await _entity(maker, "the kayak", kind="Thing")
    for obj, label in ((civic, "Civic"), (kayak, "kayak")):
        await _fact(
            maker,
            jeff,
            note_id,
            predicate="owns",
            statement=f"Jeff owns the {label}.",
            object_id=obj,
            kind="relationship",
        )

    args = {
        "entity": jeff,
        "predicate": "owns",
        "qualifier": "",
        "object": "a bicycle",
        "statement": "Jeff owns a bicycle.",
        "when": "",
    }
    out = str(await _handlers(maker)[CORRECT_FACT](args, _ctx(owner_ctx, session_id)))
    # What IS on file is still read back — the model has to be able to tell Jeff.
    assert "holds 2 values at once" in out
    assert "Civic" in out and "kayak" in out
    assert "Nothing was changed" in out
    assert "cannot single one out" in out
    # Nothing written: still exactly the two edges it started with, both live.
    rows = await _rows(maker, jeff, "owns")
    assert len(rows) == 2
    assert {r.status for r in rows} == {"active"}

    # And no `replaces` gets past it, because there is no such argument any more: a
    # retry naming a handle is the same refusal, not a write.
    retry = str(
        await _handlers(maker)[CORRECT_FACT](
            {**args, "replaces": "f1"}, _ctx(owner_ctx, session_id)
        )
    )
    assert "cannot single one out" in retry
    assert len(await _rows(maker, jeff, "owns")) == 2


@pytest.mark.asyncio
async def test_correcting_to_another_entitys_id_writes_an_edge_not_a_dangling_uuid(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """`correct_fact.tool` offers an id as the `object` when the right answer is a person
    or an organization, and it has to mean an EDGE.

    Passed through raw it was resolved against the writer's handle table — which held
    only the subject — so an id never matched. The row landed with
    `object_entity_id = NULL`, the bare uuid as its literal value, and `pinned=True`,
    while the real edge was superseded and the model was told "ok … replaced". Pinned is
    what made it terminal: nothing could auto-correct it, and `read_entity` and the wiki
    both rendered "Jeff works for f458b192-…"."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins (edge)")
    acme = await _entity(maker, "Acme", kind="Organization")
    globex = await _entity(maker, "Globex", kind="Organization")
    old = await _fact(
        maker,
        jeff,
        note_id,
        predicate="worksFor",
        statement="Jeff works for Acme.",
        object_id=acme,
        kind="relationship",
    )

    out = str(
        await _handlers(maker)[CORRECT_FACT](
            {
                "entity": jeff,
                "predicate": "worksFor",
                "qualifier": "",
                "object": globex,  # the ID of the right answer
                "statement": "Jeff works for Globex.",
                "when": "",
            },
            _ctx(owner_ctx, session_id),
        )
    )
    assert "Globex" in out
    rows = {str(f.id): f for f in await _rows(maker, jeff, "worksFor")}
    assert rows[old].status == "superseded"
    live = [f for f in rows.values() if f.status == "active"]
    assert len(live) == 1
    # A real edge, not a uuid pretending to be a value.
    assert live[0].object_entity_id is not None
    assert str(live[0].object_entity_id) == globex
    assert globex not in json.dumps(live[0].value_json or {})
    assert live[0].pinned is True


@pytest.mark.asyncio
async def test_an_object_id_the_turn_cannot_see_is_refused_and_nothing_is_written(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """The object goes through the SAME scope gate as the subject. The write session runs
    at full owner scope (constraint 2), so an unchecked object id would be a way to point
    a general note's thread at a health entity and get it named back."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins (unseen object)")
    clinic = await _entity(maker, "Bay Clinic", domain="health", kind="Organization")

    out = str(
        await _handlers(maker)[CORRECT_FACT](
            {
                "entity": jeff,
                "predicate": "treatedBy",
                "qualifier": "",
                "object": clinic,
                "statement": "Jeff is treated by Bay Clinic.",
                "when": "",
            },
            _ctx(owner_ctx, session_id),  # general-scoped
        )
    )
    assert "not an entity this conversation can see" in out
    assert await _rows(maker, jeff, "treatedBy") == []


@pytest.mark.asyncio
async def test_the_turns_read_scope_is_the_ceiling_on_what_can_be_corrected(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """The write session runs at FULL owner scope (constraint 2 — a floored fact write is
    refused by RLS outright, and layer-1 resolution carries no domain predicate), so the
    firewall cannot live there. It lives on the ADDRESS: the entity is resolved under the
    TURN's own scopes, so the only rows this tool can be pointed at are the ones
    `find_entity`/`read_entity` could already have shown it."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    clinical = await _entity(maker, "Dr. Patel", domain="health")

    out = str(
        await _handlers(maker)[CORRECT_FACT](
            {
                "entity": clinical,
                "predicate": "worksAt",
                "qualifier": "",
                "object": "Kaiser",
                "statement": "Dr. Patel works at Kaiser.",
                "when": "",
            },
            _ctx(owner_ctx, session_id),  # general-scoped, as a general note's thread is
        )
    )
    assert "not an entity this conversation can see" in out
    assert not await _rows(maker, clinical, "worksAt")


@pytest.mark.asyncio
async def test_the_correction_budget_counts_down_across_calls_and_then_stops(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """`CORRECT_CALL_BUDGET` is engine-side precisely because "a prompt-stated cap does
    not hold" (`graphwritetools`), and for a wave it did not hold either: the handler
    built a fresh `NoteGraphWriter` per call, so `ToolCallBudget(6)` was re-created every
    time. Seven consecutive corrections each reported "5 calls left" and the seventh
    still wrote.

    One writer per CONVERSATION is the fix, so the count is asserted on the sequence, not
    on one call — and the seventh must be refused rather than merely under-reported."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins (budget)")
    # ONE handlers dict, as the process holds one registry — building a second per call
    # is the very thing that hid the bug.
    handlers = _handlers(maker)

    remaining = []
    for i in range(6):
        out = str(
            await handlers[CORRECT_FACT](
                {
                    "entity": jeff,
                    "predicate": f"nickname{i}",
                    "qualifier": "",
                    "object": f"value {i}",
                    "statement": f"Jeff's nickname{i} is value {i}.",
                    "when": "",
                },
                _ctx(owner_ctx, session_id),
            )
        )
        remaining.append(out.rsplit("correct_fact: ", 1)[1].split(" calls left")[0])
    assert remaining == ["5", "4", "3", "2", "1", "0"]

    seventh = str(
        await handlers[CORRECT_FACT](
            {
                "entity": jeff,
                "predicate": "nickname6",
                "qualifier": "",
                "object": "value 6",
                "statement": "Jeff's nickname6 is value 6.",
                "when": "",
            },
            _ctx(owner_ctx, session_id),
        )
    )
    assert "out of budget" in seventh
    assert await _rows(maker, jeff, "nickname6") == []

    # A DIFFERENT conversation gets its own budget — the cap is per note, not per box.
    other_note = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, other_note, tmp_path)
    other_session = await _conversation(maker, owner_ctx, other_note)
    fresh = str(
        await handlers[CORRECT_FACT](
            {
                "entity": jeff,
                "predicate": "nickname6",
                "qualifier": "",
                "object": "value 6",
                "statement": "Jeff's nickname6 is value 6.",
                "when": "",
            },
            _ctx(owner_ctx, other_session),
        )
    )
    assert "correct_fact: 5 calls left" in fresh


# --- the unattended pair, on the reply turn -----------------------------------


@pytest.mark.asyncio
async def test_the_reply_turn_can_resolve_and_assert_and_what_it_asserts_is_not_pinned(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """D8's on-reply set is a SUPERSET, and for a wave two of its names had no handler on
    the only registry a reply turn consults — so neither was offered and neither could
    dispatch. The consequence was not a missing feature. `correct_fact` was then the
    turn's only write verb, and a correction at an empty address commits
    `insert_pinned=True`, so every new fact the owner mentioned in passing was pinned
    against every later note.

    Both halves are asserted: the pair really does dispatch, and what `assert_fact`
    writes is a live, UNPINNED fact a later note can still supersede."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    handlers = _handlers(maker)
    ctx = _ctx(owner_ctx, session_id)

    resolved = str(
        await handlers["resolve_entity"](
            {"entities": [{"surface": "Dr. Patel", "kind": "person"}]}, ctx
        )
    )
    assert "e1" in resolved

    out = await handlers["assert_fact"](
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "CTO",
                    "statement": "Dr. Patel is the CTO.",
                    "when": "",
                    "quote": "Kaiya is seen by Dr. Patel",
                }
            ]
        },
        ctx,
    )
    assert isinstance(out, ToolOutput) and len(out.facts) == 1
    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(out.facts[0].fact_id)))
        ).scalar_one()
    assert row.status == "active"
    # NOT pinned — the whole point. A later note may still supersede this.
    assert row.pinned is False


@pytest.mark.asyncio
async def test_an_unprompted_reply_may_not_pin_a_correction_out_of_a_real_quote(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """R3's third review, finding 1 — the PERMANENT shape of the O16 deferral, closed.

    The elevation in `_assert_one` turns an ATTESTED element of an `owner_correction`
    note into `correction=True`, which force-supersedes and PINS at confidence 1.0. And
    `_attests` checks only that the quote STRING is somewhere in the note's chunks —
    never that it supports the object. So on a correction note the owner reopens days
    later, a `close_reading` element quoting a line the note really contains, carrying a
    value he typed only into the thread, landed active + pinned: `sweep_note` spares a
    pinned row, no later note supersedes one, and no correction note can address it.
    Nothing in the system could ever take it back, which is what O16 calls worse than the
    loss it stands in for.

    The gate is the turn's own allowlist, where `narrow_for_unprompted_reply` already
    landed (`_ctx(answering=False)`), and both halves are on one note:

    - the unprompted turn's element commits UNPINNED — still the O16 loss shape,
      deliberately still open, because it is falsifiable by the next reading and swept
      when one comes;
    - the answering turn's element, same note and same quote, still pins, because there
      the owner's words ARE the note's text and the elevation is what it is for."""
    body = (
        "Correction: Kaiya's dentist is not Dr. Patel. Kaiya is seen by Dr. Marsh for orthodontics."
    )
    note_id = await make_note(maker, domain="general", body=body)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        # What `file_correction` / `POST /api/wiki/{id}/corrections` stamp. The capture
        # API has no field for it, so the row is the only place it can come from.
        await s.execute(
            text(
                "UPDATE app.notes SET provenance = 'owner_correction' WHERE id = CAST(:n AS uuid)"
            ),
            {"n": note_id},
        )
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    handlers = _handlers(maker)
    quote = "Kaiya is seen by Dr. Marsh for orthodontics."

    unprompted = _ctx(owner_ctx, session_id, answering=False)
    await handlers["resolve_entity"](
        {"entities": [{"surface": "Kaiya of the reopened correction", "kind": "person"}]},
        unprompted,
    )
    said_in_chat = await handlers["close_reading"](
        {
            "title": "A correction",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "dentist",
                    # The value the owner typed in the thread and the note never received.
                    # The QUOTE is real, which is the whole trap: attestation is a string
                    # check, not a check that the line supports this object.
                    "object": "Dr. Ashcote",
                    "statement": "Kaiya's dentist is Dr. Ashcote.",
                    "when": "",
                    "quote": quote,
                }
            ],
        },
        unprompted,
    )
    assert isinstance(said_in_chat, ToolOutput) and len(said_in_chat.facts) == 1
    row = await _fact_row(maker, uuid.UUID(said_in_chat.facts[0].fact_id))
    assert row.pinned is False, "an unprompted reply minted a fact nothing can ever correct"
    # And the model is told, because a row it believes is pinned is one it reports as
    # settled to the owner.
    assert "NOT as a pinned correction" in str(said_in_chat)

    # The control, one flag apart: on the turn whose answer became the note's text the
    # elevation still fires. Without this the fix would read as "corrections stopped
    # pinning", which is a different and worse bug.
    answering = _ctx(owner_ctx, session_id)
    answered = await handlers["close_reading"](
        {
            "title": "A correction",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "orthodontist",
                    "object": "Dr. Marsh",
                    "statement": "Kaiya is seen by Dr. Marsh.",
                    "when": "",
                    "quote": quote,
                }
            ],
        },
        answering,
    )
    assert isinstance(answered, ToolOutput) and len(answered.facts) == 1
    elevated = await _fact_row(maker, uuid.UUID(answered.facts[0].fact_id))
    assert elevated.pinned is True
    assert elevated.status == "active"


@pytest.mark.asyncio
async def test_the_reply_turns_four_verbs_share_one_handle_table(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """One writer per conversation, asserted where it is visible to the model: a handle
    minted by `resolve_entity` is still an address on a LATER call, and `correct_fact`
    adopting the same entity reuses that handle rather than minting `e2` for it.

    Per-call writers made both false — every call started at `e1` and the model was
    reading a fresh vocabulary each time."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    handlers = _handlers(maker)
    ctx = _ctx(owner_ctx, session_id)

    await handlers["resolve_entity"](
        {"entities": [{"surface": "Dr. Patel", "kind": "person"}]}, ctx
    )
    second = str(
        await handlers["resolve_entity"](
            {"entities": [{"surface": "Kaiya", "kind": "person"}]}, ctx
        )
    )
    # A second call continues the numbering rather than restarting it.
    assert "e2" in second

    # And a fact written against the FIRST call's handle still lands.
    out = await handlers["assert_fact"](
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "doctor",
                    "statement": "Dr. Patel is a doctor.",
                    "when": "",
                    "quote": "Kaiya is seen by Dr. Patel",
                }
            ]
        },
        ctx,
    )
    assert isinstance(out, ToolOutput) and len(out.facts) == 1
    assert "no such handle" not in str(out)


# --- merge_entities -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fold_is_staged_and_nothing_is_folded(maker, tmp_path, owner_ctx) -> None:  # noqa: F811
    """Constraint 12. The tool raises a Proposal tied to this conversation's session (so
    it lands on the review inbox's notes tab beside its own thread) and leaves the graph
    exactly as it found it — both entities live, no tombstone, nothing repointed."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    a = await _entity(maker, "Dana Whitfield")
    b = await _entity(maker, "Dana W")

    out = await _handlers(maker)[MERGE_ENTITIES](
        {"entity_a": a, "entity_b": "Dana W", "reason": "Jeff says they are one person."},
        _ctx(owner_ctx, session_id),
    )
    body = str(out)
    assert "Staged" in body
    assert "not allowed to make on its own" in body
    assert "do not say they are merged" in body.lower()
    assert isinstance(out, ToolOutput) and out.proposal is not None
    assert out.proposal.kind == "merge"

    # Nothing folded: the enact is Jeff's, and it is a full-owner session.
    async with scoped_session(maker, SYSTEM_CTX) as s:
        statuses = {
            row.status
            for row in (
                await s.execute(
                    select(Entity.status, Entity.merged_into_id).where(
                        Entity.id.in_([uuid.UUID(a), uuid.UUID(b)])
                    )
                )
            ).all()
        }
    assert statuses == {"confirmed"}

    # The staged card carries the two ids structurally and asserts NO direction — the
    # survivor is `plan_merge`'s at enact, never the model's.
    row, nodes = await ProposalRepo(maker).load(owner_ctx, out.proposal.proposal_id)
    node = nodes[0]
    assert node.op == "merge_entities"
    assert {node.preview["entity_a"], node.preview["entity_b"]} == {a, b}
    assert "keep" not in node.preview and "survivor" not in node.preview
    # Tied to the thread that raised it, which is what puts it on the notes tab.
    assert row.session_id == session_id


@pytest.mark.asyncio
async def test_the_narrowed_conversation_could_not_have_folded_even_if_it_tried(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """Why staging is the only shape available, rather than a policy choice: the fold
    itself fails closed on the very session the reply turn runs under. This is the
    guard the tool is built around, asserted at the layer that enforces it."""
    from jbrain.analysis.entities import MergeScopeError

    a = await _entity(maker, "Dana Whitfield")
    b = await _entity(maker, "Dana W")
    narrowed = SessionContext(
        principal_id=owner_ctx.principal_id,
        principal_kind="owner",
        owner_scoped=True,
        domain_scopes=("general",),
    )
    async with scoped_session(maker, narrowed) as s:
        with pytest.raises(MergeScopeError):
            await merge_entity_pair(s, keep=uuid.UUID(a), gone=uuid.UUID(b))


@pytest.mark.asyncio
async def test_an_already_folded_pair_is_one_entity_not_a_second_fold(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """`entities.live_entity_by_id` follows `merged_into_id` to the survivor, so a
    tombstoned id resolves to what it became. Both ids landing on the same live row is
    "already the same entity" — never a second card that would fold onto a tombstone."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    keep = await _entity(maker, "Dana Whitfield")
    gone = await _entity(maker, "Dana W")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await merge_entity_pair(s, keep=uuid.UUID(keep), gone=uuid.UUID(gone))

    out = str(
        await _handlers(maker)[MERGE_ENTITIES](
            {"entity_a": keep, "entity_b": gone, "reason": ""},
            _ctx(owner_ctx, session_id),
        )
    )
    assert "already the same entity" in out


@pytest.mark.asyncio
async def test_a_pair_jeff_already_called_distinct_is_refused_not_staged(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """A rejected merge writes a permanent `distinct_from`, and the enact refuses on it.
    Checking here means Jeff is not handed a card whose only outcome is a refusal of a
    question he already answered."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    a = await _entity(maker, "Dana Whitfield")
    b = await _entity(maker, "Dana Wu")
    lo, hi = sorted((uuid.UUID(a), uuid.UUID(b)))
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.entity_distinctions (id, entity_a, entity_b, reason,"
                " domain_code) VALUES (:id, :a, :b, 'Jeff said so', 'general')"
            ),
            {"id": str(uuid.uuid4()), "a": str(lo), "b": str(hi)},
        )

    out = str(
        await _handlers(maker)[MERGE_ENTITIES](
            {"entity_a": a, "entity_b": b, "reason": ""}, _ctx(owner_ctx, session_id)
        )
    )
    assert "different people or things" in out
    assert "Nothing was staged" in out


@pytest.mark.asyncio
async def test_a_scope_change_rebuild_carries_the_reading_not_just_the_budgets(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """`close_reading` on the reply turn, and the state a rebuilt writer must not drop.

    The cache is keyed on the turn's read SCOPES as well as its conversation, so a turn
    whose scopes changed gets a fresh `NoteGraphWriter` — and the rule the rebuild has
    always carried is "a rebuild must never be a way to buy calls". `Reading` joins the
    budgets for that reason and for a second one that is worse: `clamped` says this
    thread's reading is a PREFIX of the note, so a rebuild that dropped it would launder
    an incomplete reading into a complete-looking one, which is precisely the input the
    settle's gate exists to refuse."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    handlers = _handlers(maker)
    ctx = _ctx(owner_ctx, session_id)

    await handlers["resolve_entity"]({"entities": [{"surface": "Kaiya", "kind": "person"}]}, ctx)
    out = await handlers["close_reading"](
        {
            "title": "Kaiya's new medication",
            "tags": ["kaiya"],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "student",
                    "statement": "Kaiya is a student.",
                    "when": "",
                    "when_end": "",
                    "quote": "Kaiya is seen by Dr. Patel",
                }
            ],
        },
        ctx,
    )
    assert isinstance(out, ToolOutput) and len(out.facts) == 1
    assert "close_reading: 5 calls left this note" in str(out)

    # The SAME conversation on a widened turn: a different cache key, so a new writer.
    widened = str(
        await handlers["close_reading"](
            {"title": "", "tags": [], "facts": []},
            _ctx(owner_ctx, session_id, scopes=("general", "health")),
        )
    )
    # Nothing to record, so nothing landed — but the budget did not reset with the
    # rebuild, and neither did the reading the first call accumulated.
    assert "close_reading takes" in widened or "4 calls left" in widened
    again = str(
        await handlers["close_reading"](
            {
                "title": "More on Kaiya",
                "tags": [],
                "facts": [
                    {
                        "subject": "e1",
                        "predicate": "allergy",
                        "object": "penicillin",
                        "statement": "Kaiya is allergic to penicillin.",
                        "when": "",
                        "when_end": "",
                        "quote": "Kaiya is seen by Dr. Patel",
                    }
                ],
            },
            _ctx(owner_ctx, session_id, scopes=("general", "health")),
        )
    )
    # The rebuilt writer continued the count instead of starting over…
    assert "close_reading: 4 calls left this note" in again
    # …and kept the title the FIRST call gave the note, which is the reading itself
    # surviving the rebuild.
    assert 'titled "Kaiya\'s new medication"' in again
