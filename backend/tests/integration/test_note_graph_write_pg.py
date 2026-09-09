"""`resolve_entity` / `assert_fact` against real Postgres — the two tools that write.

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
from typing import Any

import pytest
from sqlalchemy import select

from jbrain.agent.graphwritetools import NoteGraphWriter, NoteTarget
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.analysis.converse import ledger_rows
from jbrain.analysis.entities import normalize_alias
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, EntityMention, Fact
from jbrain.models.note_conversation import NoteConversationRepo
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import (  # noqa: F401
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
) -> NoteGraphWriter:
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    async with scoped_session(maker, SYSTEM_CTX) as s:
        created = (await s.execute(select(Entity.id).where(Entity.id == uuid.UUID(int=0)))).first()
    assert created is None  # a sanity read that also proves the fixture DB is up
    return NoteGraphWriter(
        maker,
        AnalysisPipeline(maker, router),
        target=await _target(maker, note_id, domain),
        write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
        read_scopes=read_scopes,
    )


async def _target(maker, note_id: str, domain: str) -> NoteTarget:  # noqa: F811
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
