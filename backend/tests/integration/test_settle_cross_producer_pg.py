"""Two producers, one note, one whole-note settle — can `integrate_note`'s sweep still
reach the note conversation's facts?

`note.ingested` drives BOTH `integrate_note` and `note_converse` on every ordinary note
(`worker.py`, AGENT_INGEST_CONVERSATION_PLAN D13), and they write to the SAME note under
different extractors: `f"{provider}:{model}"` for the analyzer, `note_ingest` /
`note_ingest_reply` for the conversation. Only the analyzer settles. Its sweep used to be
keyed on `note_id` alone, so every unpinned, non-derived, active fact of the note that was
not in the analyzer's own `touched` set was retracted, whoever wrote it — and the same
settle's `_reconcile_mentions` deleted the co-writer's mention rows with it.

That was shipped loss, not a prospective race, and `ingest/emr/ownership.py` recorded it
as prospective on the reasoning that "the conversation adds no sweep of its own today".
True and beside the point: a producer does not need a sweep to LOSE, only a co-writer
that has one. It is closed by scoping both destructive halves to the producer that
stamped the row (`analysis/settle_owner.py`, docs/plans/SETTLE_OWNERSHIP.md S1).

Four tests, and each one holds a different half of that claim honest:

- the premise — the ordinary (non-correction) conversation write lands unpinned,
  non-derived and active, i.e. squarely inside the sweep's predicate. If `_assert_one`
  ever started pinning ordinary facts, the survival tests below would pass for the wrong
  reason, and this is what would say so.
- the loss itself, which was the strict xfail this file shipped with: the conversation's
  fact (and its mentions) survive a settle it was never told about.
- the owner's reply turn, written under the conversation's SECOND extractor string,
  surviving the re-ingest that re-runs the analyzer.
- the hazard the fix must not introduce: the analyzer still retracts its OWN stale facts
  after the model behind its extractor string changes.

The LLM is faked throughout (CLAUDE.md #5): `apply_intent` consumes a pre-built intent,
and the writer's router is a stub its resolver never calls out through.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from jbrain.agent.graphwritetools import NoteGraphWriter, NoteTarget
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.analysis.arbiter import plan_intent
from jbrain.analysis.intent import AttestedSpan, EntityResolution
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.settle_owner import ANALYZER, CONVERSATION
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import EntityMention, Fact
from jbrain.models.notes import Note
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_apply_intent_pg import (
    _SURFACE,
    _fact,
    _intent,
    _load_chunks,
)
from tests.integration.test_extraction_pg import (  # noqa: F401
    ingest,
    make_note,
    maker,
)
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _body(person: str, org: str) -> str:
    """Both producers' material in one body: the conversation quotes the first
    paragraph, the analyzer's intent attests the second."""
    return (
        f"Coffee with {person} at Ritual this morning. She is allergic to shellfish.\n\n"
        f"Notes about {org} and its plans."
    )


def _names() -> tuple[str, str]:
    """A person and an organization no other test in the shared fixture database has
    touched.

    Not cosmetic: resolution is BY NAME, and `decide()` refreshes an identical head IN
    PLACE — so a second test reusing "Dana" gets back the FIRST test's fact id, on the
    FIRST test's note, which no settle of this note would ever sweep. That is a
    cross-producer test that passes while the bug it names is live."""
    tag = uuid.uuid4().hex[:8]
    return f"Dana Crossfire {tag}", f"Globexit {tag}"


ANALYZER_EXTRACTOR = "xai:grok-4.3"
"""What `integrate_note` passes: `f"{provider}:{model}"` (`pipeline.py`). Spelled out
here because the whole question is whether the sweep can tell it from `note_ingest`."""


async def _writer(
    maker,  # noqa: F811
    note_id: str,
    extractor: str = "note_ingest",
) -> NoteGraphWriter:
    """The conversation's graph-write tools over `note_id`, built exactly as the worker
    builds them: the shipped `AnalysisPipeline`, the default `note_ingest` extractor and
    an ORDINARY note target — `provenance="human"`, so `is_correction` is False and the
    correction elevation (which pins) is out of play.

    `extractor` is the run: `note_ingest` is the unattended pass, `note_ingest_reply`
    the owner's reply turn (`agent/replytools.py`). Two strings, one producer."""
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(
                select(Note.created_at, Note.tz_offset_minutes).where(Note.id == note_id)
            )
        ).one()
    return NoteGraphWriter(
        maker,
        AnalysisPipeline(maker, router),
        extractor=extractor,
        target=NoteTarget(
            note_id=uuid.UUID(note_id),
            domain="general",
            captured_at=row.created_at,
            tz_offset_minutes=row.tz_offset_minutes,
        ),
        write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
        read_scopes=("general",),
    )


async def _conversation_fact(
    maker,  # noqa: F811
    tmp_path,
    extractor: str = "note_ingest",
) -> tuple[str, str, str, uuid.UUID]:
    """A note, the person its first paragraph names, the organization its second names,
    and the id of one ordinary fact committed through the CONVERSATION's real write path
    — `resolve_entity` then `assert_fact`, no hand-built rows."""
    person, org = _names()
    note_id = await make_note(maker, domain="general", body=_body(person, org))
    await ingest(maker, note_id, tmp_path)
    writer = await _writer(maker, note_id, extractor)
    ctx = ToolContext(session=OWNER, scopes=("general",))
    await writer.resolve_entity({"entities": [{"surface": person, "kind": "person"}]}, ctx)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "allergy",
                    "object": "shellfish",
                    "statement": f"{person} is allergic to shellfish.",
                    "when": "",
                    "quote": "allergic to shellfish",
                }
            ]
        },
        ctx,
    )
    assert isinstance(out, ToolOutput)
    assert len(out.facts) == 1, str(out)
    return note_id, person, org, uuid.UUID(out.facts[0].fact_id)


async def _integrate(
    maker,  # noqa: F811
    note_id: str,
    org: str,
    *,
    extractor: str = ANALYZER_EXTRACTOR,
    predicate: str = "industry",
) -> None:
    """The analyzer's half of the same note: commit ONE fact of its own and settle,
    with `touched` holding only what this pass wrote.

    `apply_intent` is the highest seam that runs without a live model — it is what
    `integrate_note` calls once the arbiter has ruled — so this is the analyzer's real
    commit+settle pair, not a hand-rolled `settle_note`."""
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    intent = _intent(
        note_id,
        [EntityResolution(mention_ref="m1", mode="new", new_kind="Organization", new_name=org)],
        [
            _fact(
                "m1",
                predicate=predicate,
                statement=f"{org} is in tech",
                attested_span=AttestedSpan("c", org),
            )
        ],
    )
    chunks = await _load_chunks(maker, note_id)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await AnalysisPipeline(maker, router).apply_intent(
            session,
            note_id=uuid.UUID(note_id),
            note_domain="general",
            captured_at=datetime.now(UTC),
            chunks=chunks,
            intent=intent,
            plan=plan_intent(intent, signals={0: _SURFACE}),
            title="t",
            tags=[],
            extractor=extractor,
            settle_owner=ANALYZER,
        )


async def _fact_row(maker, fact_id: uuid.UUID) -> Fact:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (await s.execute(select(Fact).where(Fact.id == fact_id))).scalar_one()


async def _one_fact(maker, note_id: str, predicate: str) -> Fact:  # noqa: F811
    """The note's single fact on `predicate` — asserted single so a duplicate row from a
    second pass can never be mistaken for the first one surviving."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                select(Fact).where(Fact.note_id == uuid.UUID(note_id), Fact.predicate == predicate)
            )
        ).scalar_one()


async def _mentions(maker, note_id: str, owner: str) -> set[uuid.UUID]:  # noqa: F811
    """The mention ids one producer still CLAIMS on the note — what its own reconcile
    may release, and what a co-writer's must leave standing."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                select(EntityMention.id, EntityMention.settle_owners).where(
                    EntityMention.note_id == uuid.UUID(note_id)
                )
            )
        ).all()
        return {r.id for r in rows if owner in r.settle_owners}


async def _mention_at(maker, note_id: str, surface: str) -> EntityMention:  # noqa: F811
    """The note's single mention row for `surface` — single because the whole point of
    the shared-span case is that both producers land on ONE row."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                select(EntityMention).where(
                    EntityMention.note_id == uuid.UUID(note_id),
                    EntityMention.surface_text == surface,
                )
            )
        ).scalar_one()


async def _owner_statuses(maker, note_id: str, owner: str) -> set[str]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            await s.execute(
                select(Fact.status, Fact.settle_owners).where(Fact.note_id == uuid.UUID(note_id))
            )
        ).all()
        return {r.status for r in rows if owner in r.settle_owners}


async def test_an_ordinary_conversation_fact_lands_inside_the_sweeps_predicate(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The premise, pinned: an ordinary `assert_fact` write is exactly the row
    `settle_note`'s sweep selects — unpinned, no `derived_from_fact_id`, `active`.

    The correction path pins deliberately (`decide()`'s `correction` branch), which is
    why the ordinary path has to be checked separately: if it pinned too, the sweep
    could not reach it and the cross-producer test below would be vacuous."""
    note_id, _, _, fact_id = await _conversation_fact(maker, tmp_path)

    row = await _fact_row(maker, fact_id)
    # On THIS note — the id `assert_fact` hands back is an existing head's when the
    # identity key already has one, and only a row of this note is in the sweep's reach.
    assert row.note_id == uuid.UUID(note_id)
    assert row.status == "active"
    assert row.pinned is False
    assert row.derived_from_fact_id is None
    # The one field that could ever ATTRIBUTE the sweep — and the sweep does not read it.
    assert row.extractor == "note_ingest"


async def test_the_analyzers_settle_leaves_the_conversations_fact_alone(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """Two producers on one note, in the order the worker can produce: the conversation
    commits, then the analyzer commits its own fact and settles over its own `touched`.

    The conversation's fact survives, because the sweep is scoped to the rows the
    settling producer stamped (`analysis/settle_owner.py`, docs/plans/SETTLE_OWNERSHIP.md
    S1). Before that scoping the predicate named `note_id`, `pinned`,
    `derived_from_fact_id` and `status` — nothing that could tell another producer's row
    from a stale one of the analyzer's own — and this is the loss it cost.

    The mention spine is asserted beside the fact because `_reconcile_mentions` had the
    same whole-note defect and is a hard DELETE: it took the rows `_promote_corroborated`
    counts co-mentions through and the `mention_ids` an un-merge replay needs."""
    note_id, _, org, fact_id = await _conversation_fact(maker, tmp_path)
    before = await _mentions(maker, note_id, CONVERSATION)
    assert before, "the conversation's write anchors at least one mention"

    await _integrate(maker, note_id, org)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    assert row.settle_owners == [CONVERSATION]
    assert await _mentions(maker, note_id, CONVERSATION) == before
    # ...and the analyzer's own fact is live too: scoping the sweep did not cost the
    # analyzer its write, it only stopped it reaching a co-writer's.
    assert await _owner_statuses(maker, note_id, ANALYZER) == {"active"}


async def test_a_reply_turns_facts_survive_the_re_ingest_that_re_runs_the_analyzer(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The owner-facing loop, end to end: the conversation asks, the owner answers, the
    answer is appended to the note and RE-INGESTED (D6), the re-ingest fires
    `note.ingested`, and `integrate_note` settles the note again.

    The reply turn writes under `note_ingest_reply` (`agent/replytools.py`) — a second
    extractor string for the SAME producer — so it must be spared by that settle exactly
    as the unattended pass's writes are. Keying the sweep on the extractor string would
    have got this case right by accident and the unattended pass's own writes wrong; the
    producer key gets both right for one reason."""
    note_id, _, org, fact_id = await _conversation_fact(maker, tmp_path, "note_ingest_reply")
    assert (await _fact_row(maker, fact_id)).extractor == "note_ingest_reply"

    await _integrate(maker, note_id, org)

    row = await _fact_row(maker, fact_id)
    assert row.status == "active"
    # One producer, two runs: the reply turn's row carries the same key the unattended
    # pass's does, which is what makes a future conversation sweep answerable to the
    # WHOLE-conversation ledger (`models/note_conversation.py`) rather than to one run.
    assert row.settle_owners == [CONVERSATION]


async def _analyzer_also_claims(
    maker,  # noqa: F811
    note_id: str,
    *,
    entity_id: uuid.UUID,
    statement: str,
    value_json: dict | None,
    surface: str,
) -> None:
    """An analyzer pass that asserts the SAME claim the conversation already committed:
    same entity (resolved `existing`, not minted again), same predicate, same value, and
    a mention anchored on the same surface — so `decide()` refreshes the conversation's
    row in place and `_upsert_mentions` lands on its mention row.

    This is not a contrived collision. Both producers read the same note off one
    `note.ingested` event, and a salient claim ("she is allergic to shellfish") is
    exactly what both of them write down."""
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    intent = _intent(
        note_id,
        [
            EntityResolution(
                mention_ref="m1",
                mode="existing",
                proposed_entity_id=str(entity_id),
                attested_span=AttestedSpan("c", surface),
            )
        ],
        [
            _fact(
                "m1",
                predicate="allergy",
                statement=statement,
                value_json=value_json,
                attested_span=AttestedSpan("c", "allergic to shellfish"),
            )
        ],
    )
    chunks = await _load_chunks(maker, note_id)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await AnalysisPipeline(maker, router).apply_intent(
            session,
            note_id=uuid.UUID(note_id),
            note_domain="general",
            captured_at=datetime.now(UTC),
            chunks=chunks,
            intent=intent,
            plan=plan_intent(intent, signals={0: _SURFACE}),
            title="t",
            tags=[],
            extractor=ANALYZER_EXTRACTOR,
            settle_owner=ANALYZER,
        )


async def test_a_claim_both_producers_assert_survives_either_one_letting_go(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The case a single owner cannot state, and the reason `settle_owners` is a SET.

    The owner answers a clarification, the conversation commits the fact, and then the
    analyzer — reading the same note off the same event — extracts the same claim. Under
    one-owner-per-row the second writer had to either TAKE the row (and then retract it
    the next time its own extraction phrased things differently, losing the owner's
    answer silently — the exact bug this whole change is about) or leave it alone (and
    then never be able to retract its own stale output). Neither is true. Both producers
    assert it, so both claims are recorded, and the row stands until the LAST one is
    released.
    """
    note_id, person, org, fact_id = await _conversation_fact(maker, tmp_path)
    row = await _fact_row(maker, fact_id)
    assert row.settle_owners == [CONVERSATION]

    await _analyzer_also_claims(
        maker,
        note_id,
        entity_id=row.entity_id,
        statement=row.statement,
        value_json=row.value_json,
        surface=person,
    )

    # ONE row, TWO claims — the analyzer refreshed the conversation's fact in place
    # rather than minting a rival, and joined the claim instead of taking it.
    same = await _fact_row(maker, fact_id)
    assert same.status == "active"
    assert set(same.settle_owners) == {CONVERSATION, ANALYZER}

    # Now the analyzer moves on: a later pass over the same note that says nothing
    # about the allergy at all. It releases only its OWN claim.
    await _integrate(maker, note_id, org)

    after = await _fact_row(maker, fact_id)
    assert after.status == "active", "the analyzer retracted a fact the conversation asserts"
    assert after.settle_owners == [CONVERSATION]


async def test_a_span_both_producers_anchor_survives_either_reconcile(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The experiment docs/plans/SETTLE_OWNERSHIP.md names as its one uncertain point,
    run: the same surface asserted by both producers is ONE mention row
    (`_upsert_mentions` matches on (chunk, span, entity) and keeps the existing id), and
    `_reconcile_mentions` is a hard DELETE.

    So the shared span is the sharpest version of the claim-set argument — with a single
    owner, whichever producer stamped the row last could delete a span the other still
    anchors a fact to, and the fact would keep a chunk citation with no mention behind
    it. The row must survive each reconcile in turn, and go only when nobody anchors it.
    """
    note_id, person, org, fact_id = await _conversation_fact(maker, tmp_path)
    row = await _fact_row(maker, fact_id)

    mention = await _mention_at(maker, note_id, person)
    assert mention.settle_owners == [CONVERSATION]

    await _analyzer_also_claims(
        maker,
        note_id,
        entity_id=row.entity_id,
        statement=row.statement,
        value_json=row.value_json,
        surface=person,
    )

    # Same row, both claims — including through the analyzer's own reconcile, which ran
    # at the end of that pass and asserted this span.
    shared = await _mention_at(maker, note_id, person)
    assert shared.id == mention.id
    assert set(shared.settle_owners) == {CONVERSATION, ANALYZER}

    # The analyzer's next pass anchors somewhere else entirely, so its reconcile lets
    # this span go. The conversation still anchors it, so the row stays.
    await _integrate(maker, note_id, org)

    survived = await _mention_at(maker, note_id, person)
    assert survived.id == mention.id
    assert survived.settle_owners == [CONVERSATION]


async def test_the_analyzer_still_retracts_its_own_stale_facts_across_a_model_change(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The hazard the producer key exists to survive: `Fact.extractor` is
    `f"{provider}:{model}"`, and the owner changes that from the PWA (task override,
    on-box model install) while a router fallback changes it with nobody asking.

    Scoping the sweep by extractor EQUALITY would have made the previous model's rows
    unreachable by every later sweep — stale but `active`, i.e. citable, forever. So:
    two analyzer passes on one note under two different `provider:model` strings, the
    second no longer asserting what the first did. The first pass's fact must be gone.
    """
    person, org = _names()
    note_id = await make_note(maker, domain="general", body=_body(person, org))
    await ingest(maker, note_id, tmp_path)

    await _integrate(maker, note_id, org, extractor="xai:grok-4.3")
    first = await _one_fact(maker, note_id, "industry")
    assert first.status == "active"
    assert first.settle_owners == [ANALYZER]

    # A different provider AND a different model — the note now says something else
    # about the same organization, and `industry` is no longer asserted.
    await _integrate(maker, note_id, org, extractor="openai:gpt-5.2", predicate="sector")

    assert (await _fact_row(maker, first.id)).status == "retracted"
    assert (await _one_fact(maker, note_id, "sector")).status == "active"
