"""`resolve_entity` / `assert_fact` / `close_reading` against real Postgres — the tools
that write.

W3/T2a of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md. The LLM is faked (CLAUDE.md #5);
what is real is everything that decides how a write LANDS — the layered resolver,
`commit_facts`, `supersession.decide()`, the domain floor and the RLS-scoped session.
Faking those would be testing the fake: the whole design claim of this task is that the
tools add no second write path.

What each test is defending:

- a handle is the only way an entity enters a fact, and resolving writes the mention
  spine — the co-mention graph `neighborhood()` traverses is built from those rows;
- the ledger's `fact_ids` are FILLED. W2 shipped the column structurally empty with a
  warning on it, because constraint 6's settle sweep retracts every non-pinned fact of
  the note NOT in `touched`, and `touched` is this ledger: an empty one retracts the
  note's whole graph. These handlers are the first that can fill it;
- what the server did UNASKED comes back as text — replaced-and-kept-as-history,
  already-recorded, held. That is the model's only window into `decide()`;
- a batch never rolls back its successful elements. Element 2 failing must not undo 1
  and 3, or whole-note atomicity is back through the side door;
- the domain floor is not dodgeable by SPELLING. The agent writes snake_case
  predicates; the floor's table is keyed on canonical camelCase, and a clinical fact
  landing in `general` because of a separator is a firewall hole (D18);
- a quote the note does not contain still commits (D2) but at a weight that cannot
  overwrite a confident prior;
- a cross-domain entity's NAME is withheld from a conversation not scoped to its
  domain, while the handle is still returned — the duplicate-minting problem constraint
  2 describes is solved by resolving at full scope, not by telling the model everything.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from jbrain.agent.graphwritetools import NoteGraphWriter, NoteTarget
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.analysis.clarify import ledger_rows
from jbrain.analysis.entities import normalize_alias
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, EntityMention, Fact
from jbrain.models.note_conversation import NoteConversationRepo
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.pg_fixtures import (  # noqa: F401
    ingest,
    make_note,
    maker,
)
from tests.integration.test_note_conversation_rls import owner_ctx
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

BODY = (
    "Coffee with Dana Whitfield at Ritual this morning. She started at Everlane as a "
    "staff engineer in March, and she is allergic to shellfish. Her blood pressure has "
    "been running high lately."
)


def _ctx(scopes: tuple[str, ...] = ("general",)) -> ToolContext:
    """The turn context. The handlers `del` it — a graph write runs on the note's own
    write session, never the turn's narrowed read scope — so this only proves they do
    not secretly depend on it."""
    return ToolContext(session=OWNER, scopes=scopes)


async def _writer(
    maker,  # noqa: F811
    note_id: str,
    *,
    domain: str = "general",
    read_scopes: tuple[str, ...] = ("general",),
    provenance: str = "human",
) -> NoteGraphWriter:
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    async with scoped_session(maker, SYSTEM_CTX) as s:
        created = (await s.execute(select(Entity.id).where(Entity.id == uuid.UUID(int=0)))).first()
    assert created is None  # a sanity read that also proves the fixture DB is up
    return NoteGraphWriter(
        maker,
        AnalysisPipeline(maker, router),
        target=await _target(maker, note_id, domain, provenance),
        write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
        read_scopes=read_scopes,
    )


async def _target(maker, note_id: str, domain: str, provenance: str = "human") -> NoteTarget:  # noqa: F811
    from jbrain.models.notes import Note

    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(
                select(Note.created_at, Note.tz_offset_minutes).where(Note.id == note_id)
            )
        ).one()
    return NoteTarget(
        note_id=uuid.UUID(note_id),
        domain=domain,
        captured_at=row.created_at,
        tz_offset_minutes=row.tz_offset_minutes,
        provenance=provenance,
    )


async def _note(maker, tmp_path, *, domain: str = "general", body: str = BODY) -> str:  # noqa: F811
    note_id = await make_note(maker, domain=domain, body=body)
    await ingest(maker, note_id, tmp_path)
    return note_id


def _handles(out: str) -> dict[str, str]:
    """handle -> the line it was reported on."""
    return {
        line.split()[0]: line
        for line in out.splitlines()
        if line[:1] == "e" and line[1:2].isdigit()
    }


# --- resolve_entity -----------------------------------------------------------


@pytest.mark.asyncio
async def test_resolving_mints_handles_and_writes_the_mention_spine(maker, tmp_path) -> None:  # noqa: F811
    note_id = await _note(maker, tmp_path)
    writer = await _writer(maker, note_id)

    out = await writer.resolve_entity(
        {
            "entities": [
                {"surface": "Dana Whitfield", "kind": "person"},
                {"surface": "Everlane", "kind": "organization"},
                {"surface": "Ritual", "kind": "place"},
            ]
        },
        _ctx(),
    )
    text = str(out)
    assert set(_handles(text)) == {"e1", "e2", "e3"}
    assert "new entity" in text
    # The remaining budget rides every result — a prompt-stated cap does not hold.
    assert "resolve_entity: 7 calls left this note" in text
    # Entity chips, so the D3 chip and the ledger both have structured ids.
    assert isinstance(out, ToolOutput) and len(out.entities) == 3

    async with scoped_session(maker, SYSTEM_CTX) as s:
        names = set(
            (
                await s.execute(
                    select(Entity.canonical_name).where(
                        Entity.id.in_([uuid.UUID(e.entity_id) for e in out.entities])
                    )
                )
            ).scalars()
        )
        mentions = (
            await s.execute(
                select(EntityMention.surface_text).where(EntityMention.note_id == note_id)
            )
        ).scalars()
    assert names == {"Dana Whitfield", "Everlane", "Ritual"}
    # The mention spine: resolving a surface anchors it in the note (TOOL_SURFACE — this
    # is what makes the deterministic layer enough without a `note_mentions` tool).
    assert set(mentions) == {"Dana Whitfield", "Everlane", "Ritual"}


@pytest.mark.asyncio
async def test_resolving_the_same_surface_twice_returns_the_same_handle(maker, tmp_path) -> None:  # noqa: F811
    note_id = await _note(maker, tmp_path)
    writer = await _writer(maker, note_id)
    await writer.resolve_entity(
        {"entities": [{"surface": "Dana Whitfield", "kind": "person"}]}, _ctx()
    )
    again = str(
        await writer.resolve_entity(
            {"entities": [{"surface": "Dana Whitfield", "kind": "person"}]}, _ctx()
        )
    )
    assert "e1" in again and "already resolved" in again
    assert "e2" not in again


@pytest.mark.asyncio
async def test_a_blank_surface_is_a_line_not_a_crash_and_the_rest_still_land(
    maker,  # noqa: F811
    tmp_path,
) -> None:  # noqa: F811
    """`loop._dispatch` turns a raise into a generic "hit an internal error" the model
    learns nothing from, so a bad element has to come back as text."""
    note_id = await _note(maker, tmp_path)
    writer = await _writer(maker, note_id)
    text = str(
        await writer.resolve_entity(
            {
                "entities": [
                    {"surface": "  ", "kind": "person"},
                    {"surface": "Everlane", "kind": "organization"},
                ]
            },
            _ctx(),
        )
    )
    assert "err  entities[0]" in text
    assert "e1  Everlane" in text


# --- assert_fact --------------------------------------------------------------


async def _resolved(maker, tmp_path, **kw) -> tuple[str, NoteGraphWriter]:  # noqa: F811
    note_id = await _note(maker, tmp_path, **kw)
    writer = await _writer(maker, note_id, **{k: v for k, v in kw.items() if k == "domain"})
    await writer.resolve_entity(
        {
            "entities": [
                {"surface": "Dana Whitfield", "kind": "person"},
                {"surface": "Everlane", "kind": "organization"},
            ]
        },
        _ctx(),
    )
    return note_id, writer


async def _own_person(maker, tmp_path, surface: str) -> tuple[str, NoteGraphWriter]:  # noqa: F811
    """A note and a writer whose one entity is `surface`, unique to the calling test.

    The fixture database is shared across this whole file and resolution is BY NAME, so a
    test that seeds a head on "Dana Whitfield" is really seeding it on every other test's
    Dana too. Anything asserting what happened to one specific head has to own its
    person."""
    body = f"Coffee with {surface} at Ritual this morning."
    note_id = await _note(maker, tmp_path, body=body)
    writer = await _writer(maker, note_id)
    await writer.resolve_entity({"entities": [{"surface": surface, "kind": "person"}]}, _ctx())
    return note_id, writer


@pytest.mark.asyncio
async def test_a_fact_lands_through_commit_facts_and_reports_its_row(maker, tmp_path) -> None:  # noqa: F811
    note_id, writer = await _resolved(maker, tmp_path)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "worksAt",
                    "object": "e2",
                    "statement": "Dana Whitfield works at Everlane.",
                    "when": "2026-03",
                    "quote": "started at Everlane as a staff engineer in March",
                }
            ]
        },
        _ctx(),
    )
    assert isinstance(out, ToolOutput)
    # The result names the CANONICAL predicate, not the model's spelling: `worksAt` is a
    # declared drift spelling of `worksFor`, and the registry's attractor runs before the
    # write so the model sees the name the graph actually uses.
    assert str(out).startswith("ok  Dana Whitfield.worksFor → Everlane")
    assert "assert_fact: 9 calls left this note" in str(out)
    # The write chip — the ledger's `fact_ids` and the D3 chip's row.
    assert len(out.facts) == 1
    written = out.facts[0]
    assert written.domain == "general" and written.outcome == "written"

    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(written.fact_id)))
        ).scalar_one()
    assert row.note_id == uuid.UUID(note_id)
    assert row.predicate == "worksFor" and row.kind == "relationship"
    assert row.status == "active" and row.object_entity_id is not None
    # An attested quote commits at full weight; the citation is anchored, so the fact
    # can be cited by the wiki (a null chunk_id silently drops it from every article).
    assert row.confidence == 1.0
    assert row.chunk_id is not None
    assert row.valid_from is not None and row.temporal_precision == "month"


@pytest.mark.asyncio
async def test_the_same_fact_twice_is_already_recorded_not_a_duplicate(maker, tmp_path) -> None:  # noqa: F811
    _, writer = await _resolved(maker, tmp_path)
    call: dict[str, Any] = {
        "facts": [
            {
                "subject": "e1",
                "predicate": "jobTitle",
                "object": "staff engineer",
                "statement": "Dana Whitfield is a staff engineer.",
                "when": "",
                "quote": "as a staff engineer",
            }
        ]
    }
    first = await writer.assert_fact(call, _ctx())
    second = await writer.assert_fact(call, _ctx())
    assert "already recorded" in str(second)
    # Same row, refreshed in place — never a second head on the same identity key.
    assert first.facts[0].fact_id == second.facts[0].fact_id


@pytest.mark.asyncio
async def test_an_entity_id_as_the_object_is_refused_never_stored_as_a_value(
    maker,  # noqa: F811
    tmp_path,
) -> None:  # noqa: F811
    """An id-shaped `object` that no handle answers to is not a value, and storing it as
    one is the worst outcome available: the row lands with `object_entity_id = NULL` and
    a bare uuid as its literal, so `read_entity` and the wiki render "Jeff works for
    f458b192-…" — and reached through `correct_fact` it is PINNED, so nothing can
    auto-correct it. It arrived that way because the object was looked up in a handle
    table holding only the SUBJECT, where an id can never match."""
    note_id, writer = await _own_person(maker, tmp_path, "Dana Objectid")
    stranger = str(uuid.uuid4())
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "worksFor",
                    "object": stranger,
                    "statement": "Dana Objectid works for someone.",
                    "when": "",
                    "quote": "Coffee with Dana Objectid",
                }
            ]
        },
        _ctx(),
    )
    body = str(out)
    assert "is an id this conversation has not resolved" in body
    assert out.facts == ()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            (await s.execute(select(Fact).where(Fact.note_id == uuid.UUID(note_id))))
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_an_unknown_handle_is_a_result_line_and_the_batch_survives_it(
    maker,  # noqa: F811
    tmp_path,
) -> None:  # noqa: F811
    """The per-element savepoint. Element 1 failing must not undo element 0 or block
    element 2, or whole-note atomicity returns through the side door."""
    _, writer = await _resolved(maker, tmp_path)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "staff engineer",
                    "statement": "Dana Whitfield is a staff engineer.",
                    "when": "",
                    "quote": "as a staff engineer",
                },
                {
                    "subject": "e9",
                    "predicate": "livesIn",
                    "object": "Oakland",
                    "statement": "Someone lives in Oakland.",
                    "when": "",
                    "quote": "Oakland",
                },
                {
                    "subject": "e1",
                    "predicate": "allergy",
                    "object": "shellfish",
                    "statement": "Dana Whitfield is allergic to shellfish.",
                    "when": "",
                    "quote": "she is allergic to shellfish",
                },
            ]
        },
        _ctx(),
    )
    text = str(out)
    assert 'facts[1].subject "e9": no such handle. resolve_entity first.' in text
    assert len(out.facts) == 2, text
    async with scoped_session(maker, SYSTEM_CTX) as s:
        predicates = set(
            (
                await s.execute(
                    select(Fact.predicate).where(
                        Fact.id.in_([uuid.UUID(f.fact_id) for f in out.facts])
                    )
                )
            ).scalars()
        )
    assert predicates == {"jobTitle", "allergy"}


@pytest.mark.asyncio
async def test_a_quote_the_note_does_not_contain_commits_but_cannot_overwrite(
    maker,  # noqa: F811
    tmp_path,
) -> None:  # noqa: F811
    """D2 / Lever A: the fact still commits — the confidence gates are gone. What an
    unattested quote costs it is the weight the supersession low-confidence guard reads,
    so it can never silently rewrite a value the note actually stated.

    A PRIOR HEAD is seeded, because that is the only shape in which the property means
    anything and the version of this test that seeded none passed for a wave while the
    guarantee was false end to end: `graphwritetools` capped `confidence` and wrote a
    bare 1.0 into `self_confidence`, which is the field `supersession.decide()` actually
    reads. A model paraphrasing a quote — the most likely failure there is — silently
    superseded an attested fact while the result line told it the write could not
    overwrite anything.

    `self_confidence` is NOT a stored column (`models.analysis.Fact` has `confidence`
    only) — it lives on the in-flight `ExtractedFact` and reaches `decide()` through
    `pipeline`'s candidate. So the fix is asserted where it is observable: on what
    happened to the prior head, and on the line the model is handed.

    The prior head deliberately belongs to ANOTHER note: `_facts_at_key` carries no
    `note_id` predicate, so the head at risk is any note's, and under D13 a note is not
    re-derivable from the one that destroyed it."""
    note_id, writer = await _own_person(maker, tmp_path, "Dana Quotecheck")
    attested = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "homeLocation",
                    "object": "118 Pine Ave",
                    "statement": "Dana Quotecheck lives at 118 Pine Ave.",
                    "when": "",
                    "quote": "Coffee with Dana Quotecheck at Ritual",
                }
            ]
        },
        _ctx(),
    )
    prior = uuid.UUID(attested.facts[0].fact_id)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        head = (await s.execute(select(Fact).where(Fact.id == prior))).scalar_one()
    assert head.status == "active"
    assert head.confidence == pytest.approx(1.0)

    # A SECOND note's conversation, paraphrasing rather than quoting.
    other = await _note(maker, tmp_path, body="Dana Quotecheck moved, apparently.")
    second = await _writer(maker, other)
    resolved = await second.resolve_entity(
        {"entities": [{"surface": "Dana Quotecheck", "kind": "person"}]}, _ctx()
    )
    assert "e1" in _handles(str(resolved))
    out = await second.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "homeLocation",
                    "object": "412 Oak St",
                    "statement": "Dana Quotecheck lives at 412 Oak St.",
                    "when": "",
                    "quote": "she moved to 412 Oak St last year",
                }
            ]
        },
        _ctx(),
    )
    assert "quote is not in the note" in str(out)

    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(out.facts[0].fact_id)))
        ).scalar_one()
        head = (await s.execute(select(Fact).where(Fact.id == prior))).scalar_one()
    # It COMMITS (D2) — a row exists, and it is not retracted.
    assert row.status == "pending_review"
    assert row.confidence == pytest.approx(0.4)
    # And the attested prior — on a different note — is untouched and still live.
    assert head.status == "active"
    assert str(head.note_id) == note_id
    # The model is TOLD it was held, and against what. The result line is its only window
    # into `decide()`, so a line that said "recorded" here would be the same lie one layer
    # up from the one this test exists for.
    assert "held" in str(out)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind_predicate", "value_a", "value_b"),
    [
        ("jobTitle", "staff engineer", "CTO"),  # attribute
        ("homeLocation", "118 Pine Ave", "412 Oak St"),  # state
        ("favoriteColour", "green", "blue"),  # long-tail / preference-ish
    ],
)
async def test_no_kind_of_unattested_fact_supersedes_an_attested_head(
    maker,  # noqa: F811
    tmp_path,
    kind_predicate: str,
    value_a: str,
    value_b: str,
) -> None:
    """The same property across the kinds `decide()` routes differently.

    The direct `decide()` probe behind this found `state`, `preference` and
    `relationship` all superseding an attested prior on an unattested write, with only
    `attribute` protected — and that by `attribute_collision`, not by weight. One field
    is what makes the guard reachable for all of them.

    The assertion is deliberately "the prior did not LOSE", not "the prior is still
    active", because the two kinds are protected by different branches and they end
    differently. `state` reaches the low-confidence guard, which parks the candidate and
    leaves the head live. `attribute` never gets that far: its own branch fires first and
    holds BOTH sides behind an `attribute_collision` ("two birthdays is a bug, not news").
    Either way the attested value is still there for a human; `superseded` is the one
    outcome that means it was overwritten by a quote the note does not contain."""
    who = f"Dana {kind_predicate}"
    note_id, writer = await _own_person(maker, tmp_path, who)
    first = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": kind_predicate,
                    "object": value_a,
                    "statement": f"{who}: {kind_predicate} is {value_a}.",
                    # Attested: this passage really is in the note `_own_person` wrote.
                    "when": "",
                    "quote": f"Coffee with {who} at Ritual this morning",
                }
            ]
        },
        _ctx(),
    )
    prior = uuid.UUID(first.facts[0].fact_id)

    other = await _note(maker, tmp_path, body=f"Something about {who} and {value_b}.")
    second = await _writer(maker, other)
    await second.resolve_entity({"entities": [{"surface": who, "kind": "person"}]}, _ctx())
    unattested = await second.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": kind_predicate,
                    "object": value_b,
                    "statement": f"{who}: {kind_predicate} is {value_b}.",
                    "when": "",
                    "quote": "a passage this note does not contain at all",
                }
            ]
        },
        _ctx(),
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        head = (await s.execute(select(Fact).where(Fact.id == prior))).scalar_one()
        landed = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(unattested.facts[0].fact_id)))
        ).scalar_one()
    assert head.status != "superseded", f"{kind_predicate}: the attested head was overwritten"
    assert str(head.note_id) == note_id
    # And the unattested value did not quietly become the live one instead.
    assert landed.status == "pending_review", kind_predicate
    assert landed.confidence == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_an_unparseable_date_records_the_fact_undated_and_says_so(maker, tmp_path) -> None:  # noqa: F811
    _, writer = await _resolved(maker, tmp_path)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "staff engineer",
                    "statement": "Dana Whitfield is a staff engineer.",
                    "when": "last spring",
                    "quote": "as a staff engineer",
                }
            ]
        },
        _ctx(),
    )
    assert "not a date" in str(out)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(out.facts[0].fact_id)))
        ).scalar_one()
    assert row.valid_from is None


# --- the firewall -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_domain_floor_is_not_dodgeable_by_spelling(maker, tmp_path) -> None:  # noqa: F811
    """D18: the agent chooses a novel predicate's domain by CHOOSING THE PREDICATE, so
    the predicate lookup is what has to hold. The note.extract prompt taught camelCase
    and the floor's table is keyed that way; a tool-writing model emits snake_case, and
    `blood_pressure` missing the table would land a clinical fact in `general`."""
    _, writer = await _resolved(maker, tmp_path)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "blood_pressure",
                    "object": "high",
                    "statement": "Dana Whitfield's blood pressure has been running high.",
                    "when": "",
                    "quote": "Her blood pressure has been running high lately",
                }
            ]
        },
        _ctx(),
    )
    assert out.facts[0].domain == "health"
    assert "filed under health" in str(out)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(out.facts[0].fact_id)))
        ).scalar_one()
    assert row.domain_code == "health"


@pytest.mark.asyncio
async def test_a_cross_domain_entity_gets_a_handle_but_not_its_name(maker, tmp_path) -> None:  # noqa: F811
    """Constraint 2 in both halves. Resolution runs at FULL owner scope — layer 1 carries
    no domain predicate, so narrowing it would mint a duplicate of an entity the owner
    already has. What is narrowed is what comes BACK: a general note's conversation is
    told the surface resolved, never a health entity's canonical name."""
    note_id = await _note(maker, tmp_path, body="Called Patel about the trip.")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        health_entity = Entity(
            id=uuid.uuid4(),
            kind="Person",
            canonical_name="Dr. Anjali Patel",
            domain_code="health",
            status="confirmed",
        )
        s.add(health_entity)
        await s.flush()
        from jbrain.models.analysis import EntityAlias

        s.add(
            EntityAlias(
                entity_id=health_entity.id,
                alias="Patel",
                alias_norm=normalize_alias("Patel"),
                domain_code="health",
            )
        )
    writer = await _writer(maker, note_id, read_scopes=("general",))
    text = str(
        await writer.resolve_entity({"entities": [{"surface": "Patel", "kind": "person"}]}, _ctx())
    )
    assert "e1  Patel" in text
    assert "Anjali" not in text
    assert "already known" in text
    # And nothing ELSE about the row either. The withheld canonical name was never the
    # whole disclosure: `[Medication] (health)` on a general note's thread says what kind
    # of thing the owner has and which domain files it, which is the same question the
    # name answers less precisely. The handle is what the model needs; the handle is all
    # it gets.
    assert "[Person]" not in text and "(health)" not in text
    # The handle points at the EXISTING health row — no duplicate was minted.
    assert writer.lookup("e1").entity.id == health_entity.id  # type: ignore[union-attr]


# --- the ledger ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_written_fact_ids_reach_the_conversation_ledger(maker, tmp_path) -> None:  # noqa: F811
    """W2 shipped `fact_ids` structurally empty with a warning on the field, because
    constraint 6's settle sweep retracts every non-pinned fact of the note NOT in
    `touched` — and `touched` is this. An empty ledger is a retraction armed, not "the
    note says nothing"."""
    _, writer = await _resolved(maker, tmp_path)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "allergy",
                    "object": "shellfish",
                    "statement": "Dana Whitfield is allergic to shellfish.",
                    "when": "",
                    "quote": "she is allergic to shellfish",
                }
            ]
        },
        _ctx(),
    )
    # The transcript step shape the accumulator builds from the tool_result event.
    step = {
        "name": "assert_fact",
        "ok": True,
        "summary": str(out),
        "entities": [e.model_dump() for e in out.entities],
        "facts": [f.model_dump() for f in out.facts],
    }
    row = ledger_rows([step])[0]
    assert row.fact_ids == (out.facts[0].fact_id,)
    # A fact's domain is the floored/ratcheted one the write path chose, so the ledger
    # unions it in alongside the entity chips'.
    assert "general" in row.domains


@pytest.mark.asyncio
async def test_the_ledger_round_trips_the_ids_into_the_sweeps_touched_set(maker, tmp_path) -> None:  # noqa: F811
    """The other half: `NoteConversationRepo.writes()` is what `settle_note` will read as
    `touched`, so the ids have to survive the column, not just the mapper."""
    from jbrain.agent.session import AgentSessionRepo

    _, writer = await _resolved(maker, tmp_path)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "allergy",
                    "object": "shellfish",
                    "statement": "Dana Whitfield is allergic to shellfish.",
                    "when": "",
                    "quote": "she is allergic to shellfish",
                }
            ]
        },
        _ctx(),
    )
    repo = NoteConversationRepo()
    sessions = AgentSessionRepo(maker)
    note_id = str(writer._target.note_id)
    # `agent_sessions.principal_id` is a real FK, so the shared OWNER context's random id
    # will not do (the note-conversation RLS suite's own fixture).
    owner = await owner_ctx(maker)
    async with scoped_session(maker, owner) as s:
        session = await sessions.create_on(
            s, owner, domain_scopes=["general"], title="t", agent="note_ingest"
        )
        await repo.start(s, session_id=session.id, note_id=note_id, body_sha="sha")
        await repo.record_tool_call(
            s,
            session.id,
            name="assert_fact",
            args={},
            ok=True,
            domains=["general"],
            fact_ids=[f.fact_id for f in out.facts],
        )
    async with scoped_session(maker, owner) as s:
        writes = await repo.writes(s, session.id)
    assert writes.facts == {uuid.UUID(out.facts[0].fact_id)}


# --- budgets ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_call_budget_is_per_conversation_and_refuses_in_words(maker, tmp_path) -> None:  # noqa: F811
    _, writer = await _resolved(maker, tmp_path)
    writer.assert_budget.used = writer.assert_budget.limit
    out = str(await writer.assert_fact({"facts": [{"subject": "e1"}]}, _ctx()))
    assert "out of budget" in out
    async with scoped_session(maker, SYSTEM_CTX) as s:
        count = (
            await s.execute(select(Fact.id).where(Fact.note_id == writer._target.note_id))
        ).all()
    assert count == []


# --- the D3 rung's payload, from the real write path --------------------------
#
# `tests/unit/test_fact_write_contract.py` pins the SHAPE the frontend reads; these say
# the write path fills it from what actually happened. The rung shipped reading six field
# names the backend never sent, so a fixture-built ref proves nothing on its own — the
# fields below have to arrive from `commit_facts`, not from a test helper.


@pytest.mark.asyncio
async def test_a_write_decide_parked_reports_held_and_never_written(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """THE finding. `decide()` refused to make this value live — it clashes with a head
    it cannot order — and a rung that calls that "written" tells Jeff his graph says
    something it does not. `ask_owner.tool` and the persona prompt both instruct the
    model to raise exactly this case, so the screen contradicting them is worse than
    silence.

    The reviewer's reproduction fed two facts (one replaced, one held) through the
    shipped helpers and got "2 written". This is the backend half of stopping that: the
    write path reports the state, and `status` carries it."""
    _, writer = await _resolved(maker, tmp_path)
    first = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "staff engineer",
                    "statement": "Dana Whitfield is a staff engineer.",
                    "when": "2026-03",
                    "quote": "as a staff engineer",
                }
            ]
        },
        _ctx(),
    )
    assert first.facts[0].status == "written"

    second = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "principal engineer",
                    "statement": "Dana Whitfield is a principal engineer.",
                    "when": "2026-06",
                    "quote": "as a staff engineer",
                }
            ]
        },
        _ctx(),
    )
    write = second.facts[0]
    assert write.outcome == "held"
    # The reduction the renderer reads. `held` is the ONE outcome that must never widen
    # into a live state, and the tool text agrees with it in the same breath.
    assert write.status == "held"
    assert "NOT live" in str(second)
    # The edge, as the write path resolved it — not the spelling the model sent. It is
    # what the expanded rung prints as `predicate → value`.
    assert write.predicate == "jobTitle"
    assert write.value == "principal engineer"
    assert write.from_attachment is False


@pytest.mark.asyncio
async def test_a_clamped_batch_says_truncated_on_the_step_not_only_in_the_prose(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The tool tells the MODEL its batch was clamped; D3 says the step has to tell the
    OWNER too. Without it a batch cut to its first few renders as though the whole list
    landed — the step's write list is a prefix and nothing on screen says so."""
    from jbrain.agent.graphwritetools import MAX_FACTS

    _, writer = await _resolved(maker, tmp_path)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": f"likes{i}",
                    "object": f"thing {i}",
                    "statement": f"Dana Whitfield likes thing {i}.",
                    "when": "",
                    "quote": "Coffee with Dana Whitfield",
                }
                for i in range(MAX_FACTS + 3)
            ]
        },
        _ctx(),
    )
    assert out.truncated is True
    assert len(out.facts) <= MAX_FACTS

    # A batch inside the cap is NOT truncated — otherwise the flag says nothing.
    ok = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "drinks",
                    "object": "coffee",
                    "statement": "Dana Whitfield drinks coffee.",
                    "when": "",
                    "quote": "Coffee with Dana Whitfield",
                }
            ]
        },
        _ctx(),
    )
    assert ok.truncated is False


@pytest.mark.asyncio
async def test_a_fact_quoted_from_an_attachment_is_marked_from_the_attachment(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """D12, which had no producer at all: `from_attachment` appeared nowhere in the write
    path, so the chip could never say a fact came off a photo.

    The evidence is the provenance of the CHUNK the quote is attested against — the same
    deterministic check attestation itself uses. The model does not get to say where a
    fact came from, and a quote out of the note's own prose stays unmarked."""
    from jbrain.ingest.chunker import PARAGRAPH
    from jbrain.models.notes import Attachment
    from jbrain.models.notes import Chunk as ChunkRow

    note_id = await _note(maker, tmp_path)
    ocr = "Lab report: A1C 5.4 percent, drawn 12 March."
    async with scoped_session(maker, SYSTEM_CTX) as s:
        attachment = Attachment(
            note_id=uuid.UUID(note_id),
            domain_code="general",
            sha256="0" * 64,
            filename="lab.png",
            media_type="image/png",
            size_bytes=1,
        )
        s.add(attachment)
        await s.flush()
        # One more paragraph chunk of the same note carrying the attachment's text —
        # the shape `ingest_note` builds for an OCR'd page.
        seq = (
            await s.execute(
                select(ChunkRow.seq)
                .where(ChunkRow.note_id == uuid.UUID(note_id))
                .order_by(ChunkRow.seq.desc())
                .limit(1)
            )
        ).scalar_one()
        s.add(
            ChunkRow(
                note_id=uuid.UUID(note_id),
                domain_code="general",
                granularity=PARAGRAPH,
                seq=seq + 1,
                char_start=0,
                char_end=len(ocr),
                source_kind="ocr",
                attachment_id=attachment.id,
                text=ocr,
            )
        )

    writer = await _writer(maker, note_id)
    await writer.resolve_entity(
        {"entities": [{"surface": "Dana Whitfield", "kind": "person"}]}, _ctx()
    )

    from_photo = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "labResult",
                    "object": "5.4",
                    "statement": "Dana Whitfield's A1C is 5.4.",
                    "when": "",
                    "quote": "A1C 5.4 percent",
                }
            ]
        },
        _ctx(),
    )
    assert from_photo.facts[0].from_attachment is True

    from_prose = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "allergy",
                    "object": "shellfish",
                    "statement": "Dana Whitfield is allergic to shellfish.",
                    "when": "",
                    "quote": "she is allergic to shellfish",
                }
            ]
        },
        _ctx(),
    )
    assert from_prose.facts[0].from_attachment is False


@pytest.mark.asyncio
async def test_the_span_check_is_the_only_weight_the_write_carries(
    maker,  # noqa: F811
    tmp_path,
) -> None:  # noqa: F811
    """R1b deleted the model's own `confidence` field, so this is the whole of what is
    left to distrust a fact with — and it is the half the model cannot talk its way past.
    A claim of certainty on a quote the note does not contain still lands at the 0.4
    inferred ceiling; there is no longer any field with which to claim it at all.

    R0 measured what the field bought before it went: 1 silent guess in 106 runs, and it
    happened in the arm that HAS the field (§3.3/O3b). What it cost rose under one
    channel — a spurious low number parks a TRUE fact behind a hold that no card will
    ever raise — so the measurement and the cost pointed the same way.

    The ceiling is carried on the candidate's `self_confidence` as well as its
    `confidence`, because `decide()`'s low-confidence guard keys on the first alone."""
    _, writer = await _own_person(maker, tmp_path, "Dana Ceiling")
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "staff engineer",
                    "statement": "Dana Ceiling is a staff engineer.",
                    "when": "",
                    "when_end": "",
                    "quote": "a passage this note does not contain",
                    # Sent anyway: the field is gone from the schema, so a model that
                    # still writes one must not be able to raise its own weight with it.
                    "confidence": 1,
                },
                {
                    "subject": "e1",
                    "predicate": "allergy",
                    "object": "shellfish",
                    "statement": "Dana Ceiling is allergic to shellfish.",
                    "when": "",
                    "when_end": "",
                    "quote": "Coffee with Dana Ceiling at Ritual this morning.",
                    "confidence": 0.1,
                },
            ]
        },
        _ctx(),
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = {
            r.predicate: r
            for r in (
                await s.execute(
                    select(Fact).where(Fact.id.in_([uuid.UUID(w.fact_id) for w in out.facts]))
                )
            ).scalars()
        }
    assert rows["jobTitle"].confidence == pytest.approx(0.4)
    # The attested one is 1.0 despite the 0.1 the model sent: the field is not read.
    assert rows["allergy"].confidence == pytest.approx(1.0)


# --- close_reading: the whole-note reading (AGENT_INGEST_REWRITE R1) -----------


def _one_fact(subject_line: str) -> dict[str, Any]:
    """One fact both verbs can write, quoting a body `_own_person` really produced."""
    return {
        "subject": "e1",
        "predicate": "metAt",
        "object": "Ritual",
        "statement": f"{subject_line} was at Ritual.",
        "when": "",
        "when_end": "",
        "quote": f"Coffee with {subject_line} at Ritual",
    }


@pytest.mark.asyncio
async def test_a_reading_commits_the_rows_assert_fact_would(maker, tmp_path) -> None:  # noqa: F811
    """R1's headline, as a row-level comparison: `close_reading` lands BESIDE
    `assert_fact` and commits identically. It is the same `_assert_one`, the same
    `commit_facts`, the same `decide()` — the plan's whole claim is that the reading
    changes who supplies the meaning and nothing about how a write lands (constraint 5).

    Two notes and two people, because both writes are on the same identity key otherwise
    and the second would supersede the first rather than being comparable to it."""
    _, asserted = await _own_person(maker, tmp_path, "Dana Asserted")
    _, read = await _own_person(maker, tmp_path, "Dana Readrow")

    left = await asserted.assert_fact({"facts": [_one_fact("Dana Asserted")]}, _ctx())
    right = await read.close_reading(
        {
            "title": "Coffee with Dana",
            "tags": ["dana", "coffee"],
            "facts": [_one_fact("Dana Readrow")],
        },
        _ctx(),
    )
    assert len(left.facts) == len(right.facts) == 1
    assert await _row_shape(maker, left.facts[0].fact_id) == await _row_shape(
        maker, right.facts[0].fact_id
    )
    # And the result the model reads is the same shape, down to the outcome vocabulary.
    assert str(right).startswith("ok  Dana Readrow.metAt → Ritual")
    assert right.facts[0].outcome == left.facts[0].outcome


@pytest.mark.asyncio
async def test_the_reading_differs_from_assert_fact_in_exactly_three_columns(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """Where the two verbs GENUINELY differ, pinned as a property rather than left as an
    omission — and this is the fixture the identical-rows test above cannot be, because a
    quote with no schedule in it is the one case where they cannot differ at all.

    R2's acceptance is "every currently-green scenario stays green" after the harness is
    re-cut onto `close_reading`, and that claim rests on this diff being exactly three
    columns wide: the reading DATES a recurring fact (`valid_from` at the note's own
    capture day, where `assert_fact` leaves it null), calls that date a `day` rather than
    `unknown`, and binds the temporal token that carries the rule. `decide()` compares
    validity time, so a fourth column here would be a scenario that flips."""
    left_note, asserted = await _recurring_person(maker, tmp_path, "Dana Weekly")
    right_note, read = await _recurring_person(maker, tmp_path, "Dana Repeats")
    del left_note, right_note

    left = await asserted.assert_fact({"facts": [_recurring_fact("Dana Weekly")]}, _ctx())
    right = await read.close_reading(
        {"title": "Trivia night", "tags": [], "facts": [_recurring_fact("Dana Repeats")]},
        _ctx(),
    )
    before = await _row_shape(maker, left.facts[0].fact_id)
    after = await _row_shape(maker, right.facts[0].fact_id)
    differ = {k for k in before if before[k] != after[k]}
    assert differ == {"valid_from", "temporal_precision", "has_token"}
    assert (before["valid_from"], before["temporal_precision"], before["has_token"]) == (
        None,
        "unknown",
        False,
    )
    assert (after["valid_from"] is not None, after["temporal_precision"], after["has_token"]) == (
        True,
        "day",
        True,
    )


def _recurring_fact(subject_line: str) -> dict[str, Any]:
    return {
        "subject": "e1",
        "predicate": "recurrence",
        "object": "Tuesdays and Thursdays at 6pm",
        "statement": f"{subject_line} hosts trivia every Tuesday and Thursday.",
        "when": "",
        "when_end": "",
        "quote": "every Tuesday and Thursday at 6pm",
    }


async def _recurring_person(maker, tmp_path, surface: str) -> tuple[str, NoteGraphWriter]:  # noqa: F811
    note_id = await _note(
        maker, tmp_path, body=f"{surface} hosts trivia every Tuesday and Thursday at 6pm."
    )
    writer = await _writer(maker, note_id)
    await writer.resolve_entity({"entities": [{"surface": surface, "kind": "person"}]}, _ctx())
    return note_id, writer


async def _row_shape(maker, fact_id: str) -> dict[str, Any]:  # noqa: F811
    """Everything a write DECIDES about a row, minus what identifies which write it was.

    `self_confidence` and `inferred` are not stored columns — they live on the in-flight
    `ExtractedFact` and reach `decide()` through the candidate — so what is comparable
    here is what landed. `settle_owners` is in the list because it is the column R3's
    sweep scopes on: equal by construction today, and the day it is not is the day a
    reading stops being releasable by the producer that wrote it."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (await s.execute(select(Fact).where(Fact.id == uuid.UUID(fact_id)))).scalar_one()
    return {
        "predicate": row.predicate,
        "qualifier": row.qualifier,
        "kind": row.kind,
        "status": row.status,
        "assertion": row.assertion,
        "confidence": row.confidence,
        "domain_code": row.domain_code,
        "value_json": row.value_json,
        "valid_from": row.valid_from,
        "valid_to": row.valid_to,
        "temporal_precision": row.temporal_precision,
        "has_token": row.temporal_token_id is not None,
        "pinned": row.pinned,
        "settle_owners": sorted(row.settle_owners),
        "extractor": row.extractor,
        "has_object_entity": row.object_entity_id is not None,
        "has_chunk": row.chunk_id is not None,
    }


@pytest.mark.asyncio
async def test_a_reading_needs_no_confidence_field_to_commit_at_full_weight(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """§3.3: the MODEL's confidence is deleted, the ENGINE's guard is not.

    R0 ran three smudged notes through the real persona in three conditions, 106 live
    runs, and the failure the field exists to catch happened once — in the arm that HAS
    the field, which filled it with `1`. So the field guards nothing and goes. What stays
    is the ENGINE's span check, which is a signal that really fires: an attested quote
    commits at full weight with no field to say so, and the next test is the other half."""
    _, writer = await _own_person(maker, tmp_path, "Dana Nofield")
    out = await writer.close_reading(
        {"title": "Coffee", "tags": [], "facts": [_one_fact("Dana Nofield")]}, _ctx()
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(out.facts[0].fact_id)))
        ).scalar_one()
    assert row.confidence == pytest.approx(1.0)
    assert row.status == "active"


@pytest.mark.asyncio
async def test_the_engines_span_check_still_caps_a_reading(maker, tmp_path) -> None:  # noqa: F811
    """The half of the guard `confidence`'s deletion does NOT touch: a quote the note
    does not contain caps the fact at the 0.4 inferred ceiling, well under
    `supersession.LOW_CONFIDENCE`, so it cannot silently overwrite a value the note
    actually stated."""
    _, writer = await _own_person(maker, tmp_path, "Dana Unattested")
    out = await writer.close_reading(
        {
            "title": "Coffee",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "livesIn",
                    "object": "Oakland",
                    "statement": "Dana Unattested lives in Oakland.",
                    "when": "",
                    "when_end": "",
                    "quote": "a sentence this note does not contain",
                }
            ],
        },
        _ctx(),
    )
    assert "quote is not in the note" in str(out)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(select(Fact).where(Fact.id == uuid.UUID(out.facts[0].fact_id)))
        ).scalar_one()
    assert row.confidence == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_the_reading_accumulates_across_calls_and_carries_title_and_tags(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """`_upsert_tokens` was not the only producer the teardown was about to orphan:
    `note_analysis`'s title and tags are two more fields on the same call (§1's table).
    R1 does not stamp them yet — the settle moves in R3 — so what it owes is that they
    are CARRIED, unioned across the calls a long note takes, with the fact ids beside
    them for the sweep that will read them."""
    _, writer = await _own_person(maker, tmp_path, "Dana Union")
    first = await writer.close_reading(
        {
            "title": "Coffee with Dana",
            "tags": ["Dana", "coffee"],
            "facts": [_one_fact("Dana Union")],
        },
        _ctx(),
    )
    second = await writer.close_reading(
        {
            "title": "More on Dana",
            "tags": ["coffee", "ritual"],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "visits",
                    "object": "Ritual",
                    "statement": "Dana Union visits Ritual.",
                    "when": "",
                    "when_end": "",
                    "quote": "at Ritual this morning",
                }
            ],
        },
        _ctx(),
    )
    reading = writer.reading
    assert reading.calls == 2
    assert reading.title == "Coffee with Dana"  # the first call named the note
    assert reading.tags == ("dana", "coffee", "ritual")
    assert reading.fact_ids == (first.facts[0].fact_id, second.facts[0].fact_id)
    assert reading.clamped is False
    assert "close_reading: 4 calls left this note" in str(second)


@pytest.mark.asyncio
async def test_a_clamped_reading_is_reported_as_incomplete_and_latches(maker, tmp_path) -> None:  # noqa: F811
    """§2's gate clause, and the reason the clamp is promoted from a cosmetic result line
    to a signal: a clamped reading is a PREFIX of the note, and a sweep against a prefix
    retracts the tail. R1 does not sweep, so what it owes is the signal itself — said to
    the model in stronger words than `assert_fact`'s, carried on the step as `truncated`,
    and LATCHED on the reading so a clean second call cannot clear it."""
    from jbrain.agent.graphwritetools import MAX_FACTS

    _, writer = await _own_person(maker, tmp_path, "Dana Clamped")
    out = await writer.close_reading(
        {
            "title": "A long note",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": f"likes{i}",
                    "object": f"thing {i}",
                    "statement": f"Dana Clamped likes thing {i}.",
                    "when": "",
                    "when_end": "",
                    "quote": "Coffee with Dana Clamped",
                }
                for i in range(MAX_FACTS + 3)
            ],
        },
        _ctx(),
    )
    assert out.truncated is True
    assert len(out.facts) <= MAX_FACTS
    assert "this reading is INCOMPLETE" in str(out)
    assert writer.reading.clamped is True

    clean = await writer.close_reading(
        {"title": "", "tags": [], "facts": [_one_fact("Dana Clamped")]}, _ctx()
    )
    assert clean.truncated is False
    # The LATCH: the pass produced a prefix, and a later clean call does not make the
    # reading whole again.
    assert writer.reading.clamped is True


@pytest.mark.asyncio
async def test_a_call_whose_every_element_is_unreadable_latches_too(maker, tmp_path) -> None:  # noqa: F811
    """The last path that could reach a return without latching: a `facts` list the
    handler cannot read a single element of, with no title and no tags, falls out of the
    usage branch — while `_batch` has already seen the dropped elements. The model tried
    to state facts and none of them landed, which is a prefix of the note by any reading.

    The latch is unconditional now, ahead of every return in the handler, because three
    of these have been found one at a time."""
    _, writer = await _own_person(maker, tmp_path, "Dana Nulls")
    out = str(await writer.close_reading({"facts": [None, 7]}, _ctx()))
    assert "close_reading takes" in out
    assert writer.reading.clamped is True
    # And nothing was claimed to have been read: no call landed.
    assert writer.reading.calls == 0


@pytest.mark.asyncio
async def test_a_raise_escaping_the_handler_latches_the_reading(maker, tmp_path) -> None:  # noqa: F811
    """The fourth latch, at the tool seam: a call that RAISES latches too, and the raise
    still propagates.

    The other three are the engine declining a fact the model stated; this one is the
    call ending before it can decline anything — the pool refusing a connection, a
    `set_config` blip, a failed COMMIT at block exit, a cancellation mid-batch. All of
    them land between the session open and `Reading.union`, which is why the latch has to
    wrap the whole body rather than the element loop. Driven off `_load_note`, the first
    await inside the write session.

    `loop.py:_dispatch` turns the propagated raise into a recoverable observation, which
    is why latching is not optional: the model is told to try something else and may
    simply end the turn, and a pass whose earlier call landed then presents a complete,
    unclamped reading of a note it only read a prefix of."""
    _, writer = await _own_person(maker, tmp_path, "Dana Raises")
    ok = await writer.close_reading(
        {"title": "Coffee", "tags": [], "facts": [_one_fact("Dana Raises")]}, _ctx()
    )
    assert ok.truncated is False and writer.reading.clamped is False

    async def _boom(_session: object) -> list[object]:
        raise RuntimeError("the pool said no")

    writer._load_note = _boom  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(RuntimeError):
        await writer.close_reading(
            {"title": "More", "tags": [], "facts": [_one_fact("Dana Raises")]}, _ctx()
        )
    assert writer.reading.clamped is True
    # The call never landed, so it claims no reading — only that this one is a prefix.
    assert writer.reading.calls == 1


@pytest.mark.asyncio
async def test_a_reading_refused_for_budget_is_incomplete_too(maker, tmp_path) -> None:  # noqa: F811
    """The other way a reading ends up a prefix, and the one that looked clean.

    A pass that still had facts to state and was refused the call has produced exactly
    what a clamp produces — and the refusal returns before anything is recorded, so
    without this the reading would say "one call, unclamped" and the settle would read
    that as the whole note and retract the tail the budget refused to let the model
    write. `calls` does NOT move: no call landed."""
    from jbrain.agent.graphwritetools import READING_CALL_BUDGET

    _, writer = await _own_person(maker, tmp_path, "Dana Budget")
    first = await writer.close_reading(
        {"title": "Coffee", "tags": [], "facts": [_one_fact("Dana Budget")]}, _ctx()
    )
    assert first.truncated is False and writer.reading.clamped is False

    writer.reading_budget.used = READING_CALL_BUDGET
    refused = str(
        await writer.close_reading(
            {"title": "More", "tags": [], "facts": [_one_fact("Dana Budget")]}, _ctx()
        )
    )
    assert "out of budget" in refused
    assert writer.reading.clamped is True
    assert writer.reading.calls == 1
    assert writer.reading.title == "Coffee"


# --- recurrence, read out of the quote (§3.2) ---------------------------------


async def _recurring(maker, tmp_path, subject: str, body: str) -> NoteGraphWriter:  # noqa: F811
    note_id = await _note(maker, tmp_path, body=body)
    writer = await _writer(maker, note_id)
    await writer.resolve_entity({"entities": [{"surface": subject, "kind": "event"}]}, _ctx())
    return writer


async def _token_of(maker, fact_id: str):  # noqa: F811, ANN202
    from jbrain.models.analysis import TemporalToken

    async with scoped_session(maker, SYSTEM_CTX) as s:
        fact = (await s.execute(select(Fact).where(Fact.id == uuid.UUID(fact_id)))).scalar_one()
        if fact.temporal_token_id is None:
            return fact, None
        token = (
            await s.execute(select(TemporalToken).where(TemporalToken.id == fact.temporal_token_id))
        ).scalar_one()
    return fact, token


@pytest.mark.asyncio
async def test_a_reading_writes_the_recurrence_it_reads_out_of_the_quote(maker, tmp_path) -> None:  # noqa: F811
    """The capability the tool surface could not express at all, and the one R0 changed
    the design of: 0 parseable RRULEs in 228 values across two `repeats` spellings, but
    parsing the model's own attested quote recovered the rule on 198 of 200 runs. So the
    reading writes the fact and the span, and the HANDLER writes the token.

    `app.temporal_tokens` is what `appointment_projection._recurrence_rrule` reads, and
    the conversation has been passing `tokens=[]` since W3 — so a conversation-written
    recurring appointment projected as a one-off. This is that producer."""
    body = "Gym Sessions every Tuesday and Thursday at 6am at the Y on Oak St."
    writer = await _recurring(maker, tmp_path, "Gym Sessions", body)
    out = await writer.close_reading(
        {
            "title": "Gym schedule",
            "tags": ["gym"],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "recurrence",
                    "object": "Tuesdays and Thursdays at 6am",
                    "statement": "Gym Sessions repeat every Tuesday and Thursday at 6am.",
                    "when": "",
                    "when_end": "",
                    "quote": "every Tuesday and Thursday at 6am",
                }
            ],
        },
        _ctx(),
    )
    assert "repeats FREQ=WEEKLY;BYDAY=TU,TH" in str(out)
    fact, token = await _token_of(maker, out.facts[0].fact_id)
    assert token is not None, "the fact must point at the token that carries the rule"
    assert token.rrule == "FREQ=WEEKLY;BYDAY=TU,TH"
    assert token.kind == "recurrence"
    assert token.surface_phrase == "every tuesday and thursday"
    # An undated recurring note gives the rule no start, and a token must have one — so
    # it starts when the note says it, which is the note's own capture day.
    assert fact.valid_from is not None
    assert fact.status == "active"


@pytest.mark.asyncio
async def test_a_recurrence_the_parser_refuses_leaves_the_fact_committed_and_undated(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """The discard discipline, end to end. "Tuesdays until March" is a BOUNDED rule and
    the parser will not date the bound, so it discards the whole thing rather than
    writing an unbounded rule — which would put a Spanish class on the owner's calendar
    forever. What must not happen is the fact being lost with it."""
    body = "Spanish Class Tuesdays until March at the community center on Pine."
    writer = await _recurring(maker, tmp_path, "Spanish Class", body)
    out = await writer.close_reading(
        {
            "title": "Spanish class",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "recurrence",
                    "object": "Tuesdays",
                    "statement": "Spanish Class meets Tuesdays until March.",
                    "when": "",
                    "when_end": "",
                    "quote": "Tuesdays until March",
                }
            ],
        },
        _ctx(),
    )
    assert "repeats" not in str(out)
    fact, token = await _token_of(maker, out.facts[0].fact_id)
    assert token is None
    assert fact.status == "active" and fact.valid_from is None


@pytest.mark.asyncio
async def test_an_unattested_quote_states_no_schedule(maker, tmp_path) -> None:  # noqa: F811
    """The gate on the whole mechanism: a quote the note does not contain is not evidence
    of anything, so there is nothing to read a rule out of. Without this the model could
    write a recurrence the note never stated by paraphrasing one into the quote field."""
    writer = await _recurring(
        maker, tmp_path, "Book Club", "Book Club meets at Dana's place this month."
    )
    out = await writer.close_reading(
        {
            "title": "Book club",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "recurrence",
                    "object": "monthly",
                    "statement": "Book Club meets every month.",
                    "when": "",
                    "when_end": "",
                    "quote": "Book Club meets every month on the first Monday",
                }
            ],
        },
        _ctx(),
    )
    assert "repeats" not in str(out)
    _fact, token = await _token_of(maker, out.facts[0].fact_id)
    assert token is None


# --- resolve_entity, widened (§3.4 and R0's third arm) ------------------------


@pytest.mark.asyncio
async def test_resolving_hands_back_what_the_graph_already_says(maker, tmp_path) -> None:  # noqa: F811
    """R0's hardest measurement, answered in the RESULT rather than in the prompt.

    Across 144 live runs on notes that contradicted a fact another note wrote — the fact
    one `read_entity` call away, the tool bound — the agent looked 0 times and asked 0
    times, under three personas including one told to read the graph first and one told
    to ask on a contradiction. So the conflict arrives in a result it already asked for:
    the handler has the entity loaded anyway."""
    _, first = await _own_person(maker, tmp_path, "Dana Onfile")
    await first.close_reading(
        {
            "title": "Coffee",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "livesIn",
                    "object": "Oakland",
                    "statement": "Dana Onfile lives in Oakland.",
                    "when": "2019",
                    "when_end": "",
                    "quote": "Coffee with Dana Onfile at Ritual",
                }
            ],
        },
        _ctx(),
    )
    # A LATER note, and a fresh writer: the second pass is where a contradiction lands.
    note_id = await _note(maker, tmp_path, body="Dana Onfile moved to Boulder last month.")
    later = await _writer(maker, note_id)
    text = str(
        await later.resolve_entity(
            {"entities": [{"surface": "Dana Onfile", "kind": "person"}]}, _ctx()
        )
    )
    assert "already known" in text
    # The CANONICAL predicate, as the registry rewrote it on the way in — the result
    # names the graph's own spelling, which is the one a later write has to match.
    assert "on file: homeLocation — Dana Onfile lives in Oakland." in text


@pytest.mark.asyncio
async def test_the_facts_a_resolve_hands_back_are_capped(maker, tmp_path) -> None:  # noqa: F811
    """The cap is not decoration: `resolve_entity` takes up to 12 surfaces, and an entity
    with a long history would otherwise put hundreds of statements in front of a model
    that has to hold the note too. Per entity AND per call, and when it bites the result
    says so and names the read that lifts it."""
    from jbrain.agent.graphwritetools import FACTS_PER_ENTITY

    _, seed = await _own_person(maker, tmp_path, "Dana Manyfacts")
    for batch in range(2):
        await seed.close_reading(
            {
                "title": "Coffee",
                "tags": [],
                "facts": [
                    {
                        "subject": "e1",
                        "predicate": f"likes{batch}{i}",
                        "object": f"thing {batch}{i}",
                        "statement": f"Dana Manyfacts likes thing {batch}{i}.",
                        "when": "",
                        "when_end": "",
                        "quote": "Coffee with Dana Manyfacts at Ritual",
                    }
                    for i in range(8)
                ],
            },
            _ctx(),
        )
    note_id = await _note(maker, tmp_path, body="Dana Manyfacts again.")
    later = await _writer(maker, note_id)
    text = str(
        await later.resolve_entity(
            {"entities": [{"surface": "Dana Manyfacts", "kind": "person"}]}, _ctx()
        )
    )
    assert text.count("on file:") == FACTS_PER_ENTITY
    assert "more on file — read_entity" in text


@pytest.mark.asyncio
async def test_a_cross_domain_entitys_facts_are_withheld_with_its_name(maker, tmp_path) -> None:  # noqa: F811
    """Constraint 2, applied to the new half of the result. The narrowing that already
    withholds a health entity's canonical NAME from a general note's conversation has to
    withhold what the health graph SAYS about it, or the widening reopens the firewall in
    the one place the note's own cast is guaranteed to reach."""
    note_id = await _note(maker, tmp_path, body="Called Renwick about the trip.")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        health_entity = Entity(
            id=uuid.uuid4(),
            kind="Person",
            canonical_name="Dr. Anjali Renwick",
            domain_code="health",
            status="confirmed",
        )
        s.add(health_entity)
        await s.flush()
        from jbrain.models.analysis import EntityAlias

        s.add(
            EntityAlias(
                entity_id=health_entity.id,
                alias="Renwick",
                alias_norm=normalize_alias("Renwick"),
                domain_code="health",
            )
        )
        s.add(
            Fact(
                id=uuid.uuid4(),
                entity_id=health_entity.id,
                predicate="specialty",
                qualifier="",
                kind="attribute",
                statement="Dr. Anjali Renwick is an oncologist.",
                value_json={"value": "oncologist"},
                assertion="asserted",
                status="active",
                domain_code="health",
                confidence=1.0,
                reported_at=datetime(2026, 9, 1, tzinfo=UTC),
                note_id=uuid.UUID(note_id),
                extractor="test",
                prompt_version="test",
            )
        )
    writer = await _writer(maker, note_id, read_scopes=("general",))
    text = str(
        await writer.resolve_entity(
            {"entities": [{"surface": "Renwick", "kind": "person"}]}, _ctx()
        )
    )
    assert "e1  Renwick" in text
    assert "oncologist" not in text and "Anjali" not in text
    assert "on file:" not in text


@pytest.mark.asyncio
async def test_a_floored_fact_is_withheld_even_when_its_entity_is_visible(maker, tmp_path) -> None:  # noqa: F811
    """The half an entity-level check alone would miss, and the sharper of the two.

    `Me` is a `general` entity carrying floored `health` and `finance` facts — the domain
    ratchet's whole purpose — so a narrowing keyed on the SUBJECT's domain would hand a
    general note's thread the owner's medications the moment it resolved his own name.
    The narrowing is on the FACT's domain, which is where the floor put it."""
    note_id = await _note(maker, tmp_path, body="Ran into Kestrel Vane at the market.")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        person = Entity(
            id=uuid.uuid4(),
            kind="Person",
            canonical_name="Kestrel Vane",
            domain_code="general",
            status="confirmed",
        )
        s.add(person)
        await s.flush()
        for domain, predicate, statement in (
            ("general", "worksFor", "Kestrel Vane works for Everlane."),
            ("health", "medication", "Kestrel Vane takes lisinopril 10mg."),
        ):
            s.add(
                Fact(
                    id=uuid.uuid4(),
                    entity_id=person.id,
                    predicate=predicate,
                    qualifier="",
                    kind="attribute",
                    statement=statement,
                    value_json={"value": "x"},
                    assertion="asserted",
                    status="active",
                    domain_code=domain,
                    confidence=1.0,
                    reported_at=datetime(2026, 9, 1, tzinfo=UTC),
                    note_id=uuid.UUID(note_id),
                    extractor="test",
                    prompt_version="test",
                )
            )
    writer = await _writer(maker, note_id, read_scopes=("general",))
    text = str(
        await writer.resolve_entity(
            {"entities": [{"surface": "Kestrel Vane", "kind": "person"}]}, _ctx()
        )
    )
    assert "on file: worksFor — Kestrel Vane works for Everlane." in text
    assert "lisinopril" not in text and "medication" not in text


@pytest.mark.asyncio
async def test_an_ambiguous_name_names_its_candidates_and_distinguish_picks_one(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """§3.4, both halves in one flow. The `ambiguous_mention` CARD names the candidate
    entities and the tool result never did — so the agent was told "say which one" and
    shown nothing to choose between. The result names them; `distinguish` is how it
    answers, in the note's own words.

    What it cannot do is widen: a match hands the resolver a row that ALREADY matched the
    surface, so nothing here can point a fact at an entity the deterministic layer would
    not have considered."""
    note_id = await _note(
        maker, tmp_path, body="Dana Twin — the one in Boulder — is starting at Everlane."
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        boulder = Entity(
            id=uuid.uuid4(),
            kind="Person",
            canonical_name="Dana Twin",
            domain_code="general",
            status="confirmed",
            summary="cardiologist in Boulder",
        )
        oakland = Entity(
            id=uuid.uuid4(),
            kind="Person",
            canonical_name="Dana Twin",
            domain_code="general",
            status="confirmed",
            summary="staff engineer in Oakland",
        )
        s.add_all([boulder, oakland])

    writer = await _writer(maker, note_id)
    ambiguous = str(
        await writer.resolve_entity(
            {"entities": [{"surface": "Dana Twin", "kind": "person"}]}, _ctx()
        )
    )
    assert "this is ambiguous" in ambiguous
    assert "cardiologist in Boulder" in ambiguous and "staff engineer in Oakland" in ambiguous
    assert writer.lookup("e1") is None  # no handle, so no fact can be written against it

    answered = await writer.resolve_entity(
        {
            "entities": [
                {"surface": "Dana Twin", "kind": "person", "distinguish": "the one in Boulder"}
            ]
        },
        _ctx(),
    )
    assert "e1  Dana Twin" in str(answered)
    handle = writer.lookup("e1")
    assert handle is not None and handle.entity.id == boulder.id


@pytest.mark.asyncio
async def test_a_distinguish_that_does_not_separate_them_stays_ambiguous(maker, tmp_path) -> None:  # noqa: F811
    """The refusal is the safety property. A tie is the ambiguity restated, and picking
    one of them is how a fact lands on the wrong person for good — so the phrase that
    fits both resolves nothing, exactly as no phrase at all does."""
    note_id = await _note(maker, tmp_path, body="Dana Tie came by.")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        s.add_all(
            [
                Entity(
                    id=uuid.uuid4(),
                    kind="Person",
                    canonical_name="Dana Tie",
                    domain_code="general",
                    status="confirmed",
                    summary="works at Everlane",
                ),
                Entity(
                    id=uuid.uuid4(),
                    kind="Person",
                    canonical_name="Dana Tie",
                    domain_code="general",
                    status="confirmed",
                    summary="also works at Everlane",
                ),
            ]
        )
    writer = await _writer(maker, note_id)
    out = str(
        await writer.resolve_entity(
            {
                "entities": [
                    {"surface": "Dana Tie", "kind": "person", "distinguish": "works at Everlane"}
                ]
            },
            _ctx(),
        )
    )
    assert "this is ambiguous" in out
    assert writer.lookup("e1") is None


# --- the third-party surface (D10), and the two things R1 had to keep off it ----


@pytest.mark.asyncio
async def test_a_strangers_note_is_told_no_facts_about_the_owners_entities(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
) -> None:
    """D10 permits a stranger's words to cause a FACT and nothing else, and the widened
    resolve result is not a fact — it is the owner's own graph, in CONTENT, handed into a
    thread whose turn 0 a stranger wrote. The third-party set drops `search`/`read_note`/
    `relate` because that text must not be able to AIM the corpus; a resolve that answers
    with what is on file about every name the stranger chose to write is the same thing
    through a verb that stayed."""
    _, seed = await _own_person(maker, tmp_path, "Dana Stranger")
    await seed.close_reading(
        {
            "title": "Coffee",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "jobTitle",
                    "object": "staff engineer",
                    "statement": "Dana Stranger is a staff engineer.",
                    "when": "",
                    "when_end": "",
                    "quote": "Coffee with Dana Stranger at Ritual",
                }
            ],
        },
        _ctx(),
    )
    note_id = await _note(maker, tmp_path, body="Dana Stranger asked me to pass this along.")
    stranger = await _writer(maker, note_id, provenance="untrusted_origin")
    text = str(
        await stranger.resolve_entity(
            {"entities": [{"surface": "Dana Stranger", "kind": "person"}]}, _ctx()
        )
    )
    # The handle still comes back — D10 is "unrestricted in WHAT it may write".
    assert "e1  Dana Stranger" in text and "already known" in text
    assert "on file:" not in text and "staff engineer" not in text
    # And the owner's own note is unchanged: the suppression is per NOTE, not global.
    owned = await _writer(maker, note_id)
    assert "on file: jobTitle" in str(
        await owned.resolve_entity(
            {"entities": [{"surface": "Dana Stranger", "kind": "person"}]}, _ctx()
        )
    )


@pytest.mark.asyncio
async def test_a_strangers_note_cannot_write_a_repeating_schedule(maker, tmp_path) -> None:  # noqa: F811
    """The second widening R1 had to close, and the sharper one. A recurrence token is
    what `appointment_projection._recurrence_rrule` turns into a repeating entry on the
    calendar the owner's phone subscribes to — so without this clause an approved intake
    submission could put an event in his week forever, where before it could cause a
    one-off at worst. The FACT still commits, with the dates it had: the same shape as
    every other refusal on this path."""
    body = "Community Yoga runs every Tuesday and Thursday at 6am at the church hall."
    note_id = await _note(maker, tmp_path, body=body)
    stranger = await _writer(maker, note_id, provenance="untrusted_origin")
    await stranger.resolve_entity(
        {"entities": [{"surface": "Community Yoga", "kind": "event"}]}, _ctx()
    )
    out = await stranger.close_reading(
        {
            "title": "Yoga",
            "tags": [],
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "recurrence",
                    "object": "Tuesdays and Thursdays",
                    "statement": "Community Yoga runs every Tuesday and Thursday.",
                    "when": "",
                    "when_end": "",
                    "quote": "every Tuesday and Thursday at 6am",
                }
            ],
        },
        _ctx(),
    )
    assert "repeats" not in str(out)
    fact, token = await _token_of(maker, out.facts[0].fact_id)
    assert token is None
    assert fact.status == "active"
