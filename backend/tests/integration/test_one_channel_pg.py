"""One channel: the write path REPORTS, the agent DECIDES, the owner is asked.

R1b of docs/plans/AGENT_INGEST_REWRITE.md, and its acceptance is a MATRIX rather than a
feature. `supersession.decide()` used to both decide and file: it hit a conflict, chose
`pending_review`, and the pipeline wrote a `ReviewItem` the owner adjudicated somewhere
the note is not. Under one channel it reports and stops — what it could not settle is a
tool RESULT the agent reads, and the agent either re-reads the note or asks.

So each case below pins three things at once, and it is the combination that is the
claim:

1. **The row's status is unchanged.** This is what keeps plan constraint 5 honest.
   `decide()` keeps its whole authority over what LANDS — the model still cannot force a
   supersede, un-hold a held row, or set `pinned`. Only the NOTICE moved.
2. **No `review_items` row is written.** The second channel is closed, not narrowed.
3. **A result reaches the model naming the reason and the statement it clashes with** —
   including the state the write changed that the model never named: the other side of an
   attribute collision, and a reciprocal edge refused in favour of a primary head.

What deliberately still files a card is here too, because the distinction that does the
work is not how confident the writer is but WHO the notice is for: a firewall catch is a
notice ABOUT the agent, and handing it to the agent as a result would be backwards.
"""

import uuid

import pytest
from sqlalchemy import select, update

from jbrain.agent.contracts import write_status
from jbrain.db.session import scoped_session
from jbrain.models.analysis import Entity, Fact, ReviewItem
from jbrain.models.core import Subject
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import (  # noqa: F401
    ingest,
    make_note,
    maker,
)
from tests.integration.test_note_graph_write_pg import (
    _ctx,
    _note,
    _own_person,
    _writer,
)
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


async def _cards(maker, note_id: str) -> list[str]:  # noqa: F811
    """Every review card this note has, of any kind and any status.

    Deliberately unfiltered: the claim is that the note-conversation write path files
    NOTHING, not that it files fewer of one kind."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return list(
            (
                await s.execute(
                    select(ReviewItem.kind).where(ReviewItem.payload["note_id"].astext == note_id)
                )
            ).scalars()
        )


async def _row(maker, fact_id: str) -> Fact:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (await s.execute(select(Fact).where(Fact.id == uuid.UUID(fact_id)))).scalar_one()


def _fact(predicate: str, obj: str, statement: str, *, quote: str, when: str = "") -> dict:
    return {
        "subject": "e1",
        "predicate": predicate,
        "object": obj,
        "statement": statement,
        "when": when,
        "when_end": "",
        "quote": quote,
    }


# --- the matrix: a `decide()` hold is a result, not a card --------------------


@pytest.mark.asyncio
async def test_an_attribute_collision_holds_both_sides_and_files_nothing(maker, tmp_path) -> None:  # noqa: F811
    """`supersession.py`'s attribute branch — two birthdays is a bug, not news, so BOTH
    sides go to `pending_review`. It is the one hold that changes a row the model never
    named, so the result has to say so or it under-reports the write."""
    note_id, writer = await _own_person(maker, tmp_path, "Cleo Vance")
    first = await writer.assert_fact(
        {
            "facts": [
                _fact(
                    "birthDate",
                    "1990-03-03",
                    "Cleo Vance was born March 3, 1990.",
                    quote="Coffee with Cleo Vance at Ritual this morning.",
                    when="1990-03-03",
                )
            ]
        },
        _ctx(),
    )
    second_note = await _note(maker, tmp_path, body="Cleo Vance was born in November 1985.")
    second = await _writer(maker, second_note)
    await second.resolve_entity({"entities": [{"surface": "Cleo Vance", "kind": "person"}]}, _ctx())
    out = await second.assert_fact(
        {
            "facts": [
                _fact(
                    "birthDate",
                    "1985-11-12",
                    "Cleo Vance was born November 12, 1985.",
                    quote="Cleo Vance was born in November 1985.",
                    when="1985-11-12",
                )
            ]
        },
        _ctx(),
    )

    # 1. `decide()` still decides: neither value is live, both are held.
    assert (await _row(maker, out.facts[0].fact_id)).status == "pending_review"
    assert (await _row(maker, first.facts[0].fact_id)).status == "pending_review"
    # 2. and nothing reached the owner's inbox from either note.
    assert await _cards(maker, note_id) == []
    assert await _cards(maker, second_note) == []
    # 3. the result names the reason, the statement it clashes with, and the OTHER row.
    line = str(out)
    assert "held  Cleo Vance.birthDate" in line
    assert "attribute_collision" in line
    assert "Cleo Vance was born March 3, 1990." in line
    assert "was held too, so neither is live" in line
    assert "ask the owner which is right" in line


@pytest.mark.asyncio
async def test_restating_a_still_held_row_reports_held_not_ok(maker, tmp_path) -> None:  # noqa: F811
    """The failure one channel makes possible, and it needs no re-ingest to reach.

    `supersession`'s idempotent-refresh loop admits `pending_review` — a re-run must not
    mint a twin of a held row — so restating a held fact returns `refresh_id`, and
    `_upsert_fact` used to answer that with `ALREADY`. That was safe while a card stood
    behind the row. It is not safe now: `_write_line` renders `ALREADY` as
    "ok … already recorded" and `contracts.write_status` maps it to `written`, so the
    line the agent reads and the chip the OWNER reads both say a held fact is live.

    And the pass walks into it by design. `close_reading`'s sidecar requires a whole-note
    restatement ("all of it, not only what is new since your last call") and the handler
    dedups nothing, so a fact held by `assert_fact` earlier in the same pass is restated
    seconds later — making `ok` the agent's LAST word on a fact the graph does not serve.

    So a refresh that leaves the row held reports HELD, and says the thing that is
    actually true of it: restating changed nothing, and re-reading will not settle it."""
    note_id, writer = await _own_person(maker, tmp_path, "Cleo Vance")
    born_1990 = _fact(
        "birthDate",
        "1990-03-03",
        "Cleo Vance was born March 3, 1990.",
        quote="Coffee with Cleo Vance at Ritual this morning.",
        when="1990-03-03",
    )
    await writer.assert_fact({"facts": [born_1990]}, _ctx())
    second_note = await _note(maker, tmp_path, body="Cleo Vance was born in November 1985.")
    second = await _writer(maker, second_note)
    await second.resolve_entity({"entities": [{"surface": "Cleo Vance", "kind": "person"}]}, _ctx())
    held = await second.assert_fact(
        {
            "facts": [
                _fact(
                    "birthDate",
                    "1985-11-12",
                    "Cleo Vance was born November 12, 1985.",
                    quote="Cleo Vance was born in November 1985.",
                    when="1985-11-12",
                )
            ]
        },
        _ctx(),
    )
    assert (await _row(maker, held.facts[0].fact_id)).status == "pending_review"

    # The whole-note restatement `close_reading` requires, on the same note and pass.
    again = await second.assert_fact(
        {
            "facts": [
                _fact(
                    "birthDate",
                    "1985-11-12",
                    "Cleo Vance was born November 12, 1985.",
                    quote="Cleo Vance was born in November 1985.",
                    when="1985-11-12",
                )
            ]
        },
        _ctx(),
    )
    # Same row refreshed in place, still not live — no twin, and no promotion.
    assert again.facts[0].fact_id == held.facts[0].fact_id
    assert (await _row(maker, again.facts[0].fact_id)).status == "pending_review"

    line = str(again)
    assert "held  Cleo Vance.birthDate" in line
    assert "already recorded, and STILL NOT LIVE" in line
    assert "ask the owner which is right" in line
    # The two spellings that would tell the agent, and the D3 chip, the opposite.
    assert "ok  Cleo Vance.birthDate" not in line
    assert write_status(again.facts[0].outcome) == "held"
    # Still nobody's inbox.
    assert await _cards(maker, note_id) == []
    assert await _cards(maker, second_note) == []


@pytest.mark.asyncio
async def test_a_pinned_head_holds_the_new_value_and_files_nothing(maker, tmp_path) -> None:  # noqa: F811
    """`decide()`'s pinned guard: a value the owner pinned is re-flagged, never flipped.
    The agent must not argue with a pin — it asks — and constraint 5 is the reason the
    pin itself is untouchable from here."""
    note_id, writer = await _own_person(maker, tmp_path, "Pinny Reyes")
    seeded = await writer.assert_fact(
        {
            "facts": [
                _fact(
                    "homeLocation",
                    "118 Pine Ave",
                    "Pinny Reyes lives at 118 Pine Ave.",
                    quote="Coffee with Pinny Reyes at Ritual this morning.",
                )
            ]
        },
        _ctx(),
    )
    head = uuid.UUID(seeded.facts[0].fact_id)
    # The owner's own correction is the only thing that pins, and it is a REPLY-turn
    # verb; seeding the flag directly is the shortest honest way to the branch.
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(update(Fact).where(Fact.id == head).values(pinned=True))
        await s.commit()

    later = await _note(maker, tmp_path, body="Pinny Reyes moved to 412 Oak St.")
    second = await _writer(maker, later)
    await second.resolve_entity(
        {"entities": [{"surface": "Pinny Reyes", "kind": "person"}]}, _ctx()
    )
    out = await second.assert_fact(
        {
            "facts": [
                _fact(
                    "homeLocation",
                    "412 Oak St",
                    "Pinny Reyes lives at 412 Oak St.",
                    quote="Pinny Reyes moved to 412 Oak St.",
                )
            ]
        },
        _ctx(),
    )

    assert (await _row(maker, out.facts[0].fact_id)).status == "pending_review"
    pinned = await _row(maker, str(head))
    assert pinned.status == "active" and pinned.pinned is True
    assert await _cards(maker, note_id) == [] and await _cards(maker, later) == []
    line = str(out)
    assert "held  Pinny Reyes.homeLocation" in line
    assert "fact_conflict" in line and "Pinny Reyes lives at 118 Pine Ave." in line
    assert "nothing else will raise it" in line


@pytest.mark.asyncio
async def test_a_low_weight_read_is_held_and_files_nothing(maker, tmp_path) -> None:  # noqa: F811
    """The low-confidence guard, and what is left of it after R1b took the model's own
    `confidence` field away (§3.3/O3b — 1 silent guess in 106 runs, in the arm that HAS
    the field). The ENGINE's half stands: a quote the note does not contain lands at the
    0.4 inferred ceiling, which is below `LOW_CONFIDENCE`, so it commits (D2) and cannot
    overwrite the confident prior. That is a thing the agent can FIX by re-quoting, which
    is exactly why the notice belongs to it and not to the owner."""
    note_id, writer = await _own_person(maker, tmp_path, "Blurry Nakamura")
    confident = await writer.assert_fact(
        {
            "facts": [
                _fact(
                    "homeLocation",
                    "118 Pine Ave",
                    "Blurry Nakamura lives at 118 Pine Ave.",
                    quote="Coffee with Blurry Nakamura at Ritual this morning.",
                )
            ]
        },
        _ctx(),
    )
    prior = uuid.UUID(confident.facts[0].fact_id)

    later = await _note(maker, tmp_path, body="Blurry Nakamura moved, the photo is smudged.")
    second = await _writer(maker, later)
    await second.resolve_entity(
        {"entities": [{"surface": "Blurry Nakamura", "kind": "person"}]}, _ctx()
    )
    out = await second.assert_fact(
        {
            "facts": [
                _fact(
                    "homeLocation",
                    "412 Oak St",
                    "Blurry Nakamura lives at 412 Oak St.",
                    quote="a passage this note does not contain",
                )
            ]
        },
        _ctx(),
    )

    row = await _row(maker, out.facts[0].fact_id)
    assert row.status == "pending_review"
    assert row.confidence == pytest.approx(0.4)
    assert (await _row(maker, str(prior))).status == "active"
    assert await _cards(maker, note_id) == [] and await _cards(maker, later) == []
    line = str(out)
    assert "low_confidence" in line and "Blurry Nakamura lives at 118 Pine Ave." in line
    assert "NOT live" in line and "Re-read the note" in line


@pytest.mark.asyncio
async def test_a_same_instant_supersede_lands_live_and_files_nothing(maker, tmp_path) -> None:  # noqa: F811
    """`decide()`'s one card site whose row goes ACTIVE. Two values at the SAME validity
    instant have no newest-wins basis, so Lever B stays quiet and the old card was pure
    notification of a supersession that had already happened. It becomes a clause on the
    result and nothing else."""
    _, writer = await _own_person(maker, tmp_path, "Twice Okonkwo")
    out = await writer.assert_fact(
        {
            "facts": [
                _fact(
                    "homeLocation",
                    "118 Pine Ave",
                    "Twice Okonkwo lives at 118 Pine Ave.",
                    quote="Coffee with Twice Okonkwo at Ritual this morning.",
                    when="2026-03-14",
                ),
                _fact(
                    "homeLocation",
                    "412 Oak St",
                    "Twice Okonkwo lives at 412 Oak St.",
                    quote="Coffee with Twice Okonkwo at Ritual this morning.",
                    when="2026-03-14",
                ),
            ]
        },
        _ctx(),
    )
    first, second = out.facts[0], out.facts[1]
    assert (await _row(maker, second.fact_id)).status == "active"
    assert (await _row(maker, first.fact_id)).status == "superseded"
    line = str(out)
    assert "replaced Twice Okonkwo lives at 118 Pine Ave., kept as history" in line
    assert "not a clean update (fact_conflict)" in line


@pytest.mark.asyncio
async def test_a_reciprocal_refused_for_a_primary_is_reported_on_its_source(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """`_materialize_inverse`'s derived-defers-to-primary hold: a reflected edge may
    supersede another reflection but never a human-stated head. The refused reciprocal is
    a row the model never asked for and cannot address by name, so it has no result line
    of its own — it rides the fact whose reciprocal it is."""
    first_note = await _note(maker, tmp_path, body="Bo Marchetti is married to Cy Delgado.")
    first = await _writer(maker, first_note)
    await first.resolve_entity(
        {
            "entities": [
                {"surface": "Bo Marchetti", "kind": "person"},
                {"surface": "Cy Delgado", "kind": "person"},
            ]
        },
        _ctx(),
    )
    await first.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "spouse",
                    "object": "e2",
                    "statement": "Bo Marchetti's spouse is Cy Delgado.",
                    "when": "",
                    "when_end": "",
                    "quote": "Bo Marchetti is married to Cy Delgado.",
                }
            ]
        },
        _ctx(),
    )

    second_note = await _note(maker, tmp_path, body="Ada Fenn is married to Bo Marchetti.")
    second = await _writer(maker, second_note)
    await second.resolve_entity(
        {
            "entities": [
                {"surface": "Ada Fenn", "kind": "person"},
                {"surface": "Bo Marchetti", "kind": "person"},
            ]
        },
        _ctx(),
    )
    out = await second.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "spouse",
                    "object": "e2",
                    "statement": "Ada Fenn's spouse is Bo Marchetti.",
                    "when": "",
                    "when_end": "",
                    "quote": "Ada Fenn is married to Bo Marchetti.",
                }
            ]
        },
        _ctx(),
    )

    # The source edge landed; only its reflection on Bo's stream was held.
    assert (await _row(maker, out.facts[0].fact_id)).status == "active"
    assert await _cards(maker, first_note) == [] and await _cards(maker, second_note) == []
    line = str(out)
    assert "the reciprocal edge was recorded but NOT live" in line
    assert "Bo Marchetti's spouse is Cy Delgado." in line


@pytest.mark.asyncio
async def test_an_ambiguous_name_files_nothing_and_the_result_carries_the_candidates(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """`ambiguous_mention` was the one card carrying information the AGENT needed and was
    never given: `resolve_entity` refused to guess but named none of the candidates, while
    the card listed all of them for a reader who could not see the note. R1 moved the
    names onto the result; R1b removes the card, and nothing is lost by it — no handle
    comes back either way, so no fact can be written against the ambiguity."""
    note_id = await _note(maker, tmp_path, body="Dana Onechannel — the one in Boulder — called.")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        for summary in ("cardiologist in Boulder", "staff engineer in Oakland"):
            s.add(
                Entity(
                    id=uuid.uuid4(),
                    kind="Person",
                    canonical_name="Dana Onechannel",
                    domain_code="general",
                    status="confirmed",
                    summary=summary,
                )
            )

    writer = await _writer(maker, note_id)
    out = str(
        await writer.resolve_entity(
            {"entities": [{"surface": "Dana Onechannel", "kind": "person"}]}, _ctx()
        )
    )
    assert "this is ambiguous" in out
    assert "cardiologist in Boulder" in out and "staff engineer in Oakland" in out
    assert writer.lookup("e1") is None
    assert await _cards(maker, note_id) == []


# --- what still files, because the notice is ABOUT the agent ------------------


@pytest.mark.asyncio
async def test_the_cross_subject_inverse_firewall_still_files_its_card(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """`inverse_proposal`: a reciprocal that would land a fact on a DISTINCT security
    subject's stream writes nothing and proposes. It is a firewall catch, not a question
    about the note's reading — a notice ABOUT the party the control fired on — so handing
    it to that party as a result would be backwards, and R1b leaves it untouched. It is
    also the one card the note conversation can still cause, which is why it is the case
    that proves the gate is a gate rather than a delete."""
    note_id = await _note(maker, tmp_path, body="Wren Halloway is married to Sable Quist.")
    writer = await _writer(maker, note_id)
    resolved = await writer.resolve_entity(
        {
            "entities": [
                {"surface": "Wren Halloway", "kind": "person"},
                {"surface": "Sable Quist", "kind": "person"},
            ]
        },
        _ctx(),
    )
    # Make the object its own security subject, which is what the gate keys on — then
    # resolve AGAIN on a fresh writer, because a handle caches the entity as it was.
    async with scoped_session(maker, SYSTEM_CTX) as s:
        subject = Subject(id=uuid.uuid4(), display_name="Sable Quist", kind="person")
        s.add(subject)
        await s.flush()
        await s.execute(
            update(Entity)
            .where(Entity.id == uuid.UUID(resolved.entities[1].entity_id))
            .values(subject_id=subject.id)
        )
        await s.commit()
    writer = await _writer(maker, note_id)
    await writer.resolve_entity(
        {
            "entities": [
                {"surface": "Wren Halloway", "kind": "person"},
                {"surface": "Sable Quist", "kind": "person"},
            ]
        },
        _ctx(),
    )
    await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "spouse",
                    "object": "e2",
                    "statement": "Wren Halloway's spouse is Sable Quist.",
                    "when": "",
                    "when_end": "",
                    "quote": "Wren Halloway is married to Sable Quist.",
                }
            ]
        },
        _ctx(),
    )
    assert "inverse_proposal" in await _cards(maker, note_id)


@pytest.mark.asyncio
async def test_the_domain_floor_fires_silently_because_there_is_nothing_to_propose(
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The plan lists `domain_promotion` beside `inverse_proposal` as a firewall catch the
    conversation still files. It cannot: `needs_promotion` is `ratchet_domain` refusing to
    make a fact LESS restricted than its note, and the only input that could ask for that
    is a model-supplied `domain` — the one field `assert_fact` deliberately does not have
    (the firewall red-team rule). `_assert_one` passes the NOTE's domain, so the ratchet's
    two free branches are the only ones a conversation reaches.

    What does fire is the deterministic FLOOR, which raises a clinical predicate out of a
    general note — and it is silent by design, because a floor that already landed the
    fact where it belongs has nothing to propose. The card kind stays reachable from
    `commit_intent`, where a model DOES name a per-fact domain."""
    note_id, writer = await _own_person(maker, tmp_path, "Floored Ibarra")
    out = await writer.assert_fact(
        {
            "facts": [
                _fact(
                    "allergy",
                    "shellfish",
                    "Floored Ibarra is allergic to shellfish.",
                    quote="Coffee with Floored Ibarra at Ritual this morning.",
                )
            ]
        },
        _ctx(),
    )
    assert out.facts[0].domain == "health"
    assert "filed under health" in str(out)
    assert await _cards(maker, note_id) == []


def test_card_filing_is_derived_from_the_producer_and_cannot_be_forgotten() -> None:
    """Who files is not a keyword anyone can omit — it is a function of `settle_owner`,
    which every one of the four seams already requires with no default.

    A defaulted `file_review_cards` would have replayed the failure
    `tests/unit/test_settle_owner.py` pins the opposite discipline against ("a default is
    what would let a new producer inherit someone else's sweep without saying so"), in its
    strictly worse form: a fourth deterministic producer that forgot the flag would file
    NO card and have no result reader either, so a hold `decide()` refused to make live
    would vanish from both channels at once. Deriving it makes that unreachable — there is
    no argument to forget, and the safe direction (file) is what any producer that is not
    the conversation gets.

    A signature property, checked as one: the runtime behaviour on both sides of the
    derivation is what the other eight cases in this file are."""
    import inspect

    from jbrain.analysis.pipeline import AnalysisPipeline

    assert "file_review_cards" not in inspect.signature(AnalysisPipeline.commit_facts).parameters
    source = inspect.getsource(AnalysisPipeline.commit_facts)
    assert "file_review_cards = settle_owner != CONVERSATION" in source
    # And nobody re-introduces it as a caller-supplied keyword at the seam.
    assert "file_review_cards=True" not in inspect.getsource(AnalysisPipeline.commit_intent)
