"""Re-running analyze_note against real Postgres: the retraction sweep's
chain repair (a retracted fact must not keep another fact superseded) and the
stale-open-review sweep (open cards die with their facts; resolved/dismissed
are human history; pinned facts are untouchable). LLM scripted, never live."""

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
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

from jbrain.analysis.extraction import parse_datetime
from jbrain.analysis.intent import (
    AttestedSpan,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
    IntentTemporal,
)
from jbrain.analysis.settle_owner import ANALYZER, EMR
from jbrain.db.session import scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from jbrain.wiki.builder import StubRewriter, WikiBuilder
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class _NoEmbed:
    """The builder's claim query never embeds; satisfy the constructor."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def fresh_person() -> str:
    """A per-test entity name: the suite shares one database, and a reused
    name would resolve to an earlier test's entity and upsert ITS facts."""
    return f"Sarah {uuid.uuid4().hex[:8]}"


def home_fact(
    person: str,
    city: str,
    *,
    confidence: float,
    end: str | None = None,
    statement: str | None = None,
) -> dict[str, Any]:
    """`end` (with the rendering `statement` that carries it) restates the SAME
    open state and merely supplies the valid_to it lacks — the retrospective
    backfill decide() takes as an in-place interval close."""
    return {
        "predicate": "homeLocation",
        "qualifier": "",
        "kind": "state",
        "statement": statement or f"{person} moved to {city}.",
        "value_json": {"city": city},
        "assertion": "asserted",
        "entity_ref": person,
        "object_entity_ref": None,
        "temporal": {
            "phrase": "",
            "resolved_start": "2026-06-10T00:00:00-06:00",
            "resolved_end": end,
            "precision": "day",
        },
        "domain": "general",
        "confidence": confidence,
    }


def extraction(person: str, facts: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "title": "Sarah news",
            "tags": ["sarah", "relocation", "news"],
            "mentions": [{"name": person, "kind": "Person", "surface_text": "Sarah"}],
            "facts": facts,
            "temporal_tokens": [],
        }
    )


async def analyzed_note(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path, body: str, extraction_json: str
) -> str:
    """Create + ingest a note, then analyze it with the scripted extraction."""
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"rerun-{uuid.uuid4()}", domain="general", destination=None, body=body
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note.id})
    await analyze(maker, note.id, extraction_json)
    return note.id


async def analyze(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    extraction_json: str,
    intent: IntegrationIntent | None = None,
) -> None:
    # Drive the shared deterministic driver: it parses this scripted extraction and
    # commits via a name-match default intent, so re-running with a DIFFERENT extraction
    # exercises the genuine retraction/supersession sweep. `intent` replaces that default
    # for a test that needs the arbiter to HOLD a fact.
    from tests.integration.pg_fixtures import analyzer

    await analyzer(maker, [extraction_json], intent=intent).analyze_note({"note_id": note_id})


async def fact_rows(maker: async_sessionmaker[AsyncSession], *note_ids: str) -> list[dict]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, note_id, status, pinned, superseded_by, valid_to,"
                    " value_json->>'city' AS city FROM app.facts"
                    " WHERE note_id::text = ANY(:nids) ORDER BY created_at"
                ),
                {"nids": list(note_ids)},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def review_rows(
    maker: async_sessionmaker[AsyncSession], kind: str, *note_ids: str
) -> list[dict]:
    """Cards of `kind` filed for these notes — scoped so concurrent suites'
    review items never bleed into an assertion."""
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT status, payload, settle_owner FROM app.review_items"
                    " WHERE kind = :kind"
                    " AND payload->>'note_id' = ANY(:nids) ORDER BY created_at"
                ),
                {"kind": kind, "nids": list(note_ids)},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def supersession_pair(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[str, str, str]:
    """Note A asserts Denver, note B supersedes it with Boulder (SCD-2 close +
    chain link). Post-Lever-B this clean, strictly-newer state supersession enacts
    SILENTLY — history is retained but no `fact_conflict` card is filed."""
    person = fresh_person()
    note_a = await analyzed_note(
        maker,
        tmp_path,
        "Sarah just moved to Denver.",
        extraction(person, [home_fact(person, "Denver", confidence=0.85)]),
    )
    note_b = await analyzed_note(
        maker,
        tmp_path,
        "Sarah actually moved to Boulder, not Denver.",
        extraction(person, [home_fact(person, "Boulder", confidence=0.9)]),
    )
    facts = {f["city"]: f for f in await fact_rows(maker, note_a, note_b)}
    assert facts["Denver"]["status"] == "superseded"
    assert facts["Denver"]["superseded_by"] == facts["Boulder"]["id"]
    assert facts["Denver"]["valid_to"] is not None
    return person, note_a, note_b


async def test_rerun_retraction_repairs_the_chain_it_breaks(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    person, note_a, note_b = await supersession_pair(maker, tmp_path)

    # B's re-extraction no longer asserts the move: Boulder is retracted, and
    # Denver must not stay superseded-by-a-retracted-fact — restored whole,
    # interval reopened (its close came FROM the doomed fact's valid_from).
    await analyze(maker, note_b, extraction(person, []))

    facts = {f["city"]: f for f in await fact_rows(maker, note_a, note_b)}
    assert facts["Boulder"]["status"] == "retracted"
    assert facts["Denver"]["status"] == "active"
    assert facts["Denver"]["superseded_by"] is None
    assert facts["Denver"]["valid_to"] is None
    # The open conflict card referenced the retracted fact: unservable, gone.
    assert await review_rows(maker, "fact_conflict", note_a, note_b) == []


async def test_rerun_review_sweep_spares_resolved_history(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    person, note_a, note_b = await supersession_pair(maker, tmp_path)
    facts = {f["city"]: f for f in await fact_rows(maker, note_a, note_b)}
    # Lever B enacts the clean Denver→Boulder change silently (no auto card), so
    # seed the RESOLVED fact_conflict card a human's earlier decision would have
    # left behind — referencing the Boulder fact — to prove the re-extraction sweep
    # spares human history even when it retracts that card's fact.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, status, resolved_at, payload, domain_code)"
                " VALUES (gen_random_uuid(), 'fact_conflict', 'resolved', now(),"
                " cast(:payload AS jsonb), 'general')"
            ),
            {
                "payload": json.dumps(
                    {
                        "note_id": note_b,
                        "fact_a": str(facts["Denver"]["id"]),
                        "fact_b": str(facts["Boulder"]["id"]),
                    }
                )
            },
        )

    await analyze(maker, note_b, extraction(person, []))

    facts = {f["city"]: f for f in await fact_rows(maker, note_a, note_b)}
    assert facts["Boulder"]["status"] == "retracted"
    assert facts["Denver"]["status"] == "active"
    # The human's decision survives the re-run (the sweep deletes only OPEN cards),
    # even though its Boulder fact was just retracted.
    assert [r["status"] for r in await review_rows(maker, "fact_conflict", note_a, note_b)] == [
        "resolved"
    ]


async def test_rerun_never_retracts_pinned_facts(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    person = fresh_person()
    note_id = await analyzed_note(
        maker,
        tmp_path,
        "Sarah moved to Golden.",
        extraction(person, [home_fact(person, "Golden", confidence=0.9)]),
    )
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.facts SET pinned = true WHERE note_id = :nid"), {"nid": note_id}
        )

    await analyze(maker, note_id, extraction(person, []))

    (fact,) = await fact_rows(maker, note_id)
    assert fact["status"] == "active" and fact["pinned"] is True


async def test_rerun_sweeps_stale_open_ambiguous_cards_only(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    person = fresh_person()
    note_id = await analyzed_note(maker, tmp_path, "Saw Sarah and Alex.", extraction(person, []))
    other_note = str(uuid.uuid4())
    cards = [
        # Stale AND the analyzer's own: the re-extraction below no longer references
        # "Alex", and this is the only card the sweep may take.
        ("open", "Alex", note_id, ANALYZER),
        # Still referenced: must survive.
        ("open", person, note_id, ANALYZER),
        # Human history: never touched even though stale.
        ("dismissed", "Alex", note_id, ANALYZER),
        # Another note's card: out of this run's scope.
        ("open", "Alex", other_note, ANALYZER),
        # Stale, on this note, and filed by ANOTHER producer — the EMR importer, which
        # settles the same note off the same `note.ingested`. Its names are semantic
        # keys, so the `NOT IN :names` clause spares nothing here and only the filer
        # key does (migration 0197).
        ("open", "Alex", note_id, EMR),
        # Stale, on this note, and claimed by no settling producer at all: the column
        # is nullable on purpose, and NULL means no sweep may take it.
        ("open", "Alex", note_id, None),
    ]
    async with scoped_session(maker, OWNER) as s:
        for status, name, nid, owner in cards:
            await s.execute(
                text(
                    "INSERT INTO app.review_items"
                    " (id, kind, payload, status, domain_code, settle_owner)"
                    " VALUES (gen_random_uuid(), 'ambiguous_mention',"
                    " cast(:payload AS jsonb), :status, 'general', :owner)"
                ),
                {
                    "payload": json.dumps({"name": name, "note_id": nid}),
                    "status": status,
                    "owner": owner,
                },
            )

    await analyze(maker, note_id, extraction(person, []))

    remaining = {
        (r["status"], r["payload"]["name"], r["payload"]["note_id"], r["settle_owner"])
        for r in await review_rows(maker, "ambiguous_mention", note_id, other_note)
    }
    assert remaining == {
        ("open", person, note_id, ANALYZER),
        ("dismissed", "Alex", note_id, ANALYZER),
        ("open", "Alex", other_note, ANALYZER),
        ("open", "Alex", note_id, EMR),
        ("open", "Alex", note_id, None),
    }


async def rewritten_reingest(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path, note_id: str, body: str
) -> None:
    """Re-ingest the note over a REWRITTEN body, which is what nulls `facts.chunk_id`.

    An unchanged re-ingest no longer does: `ingest.carryover` keeps a rebuilt chunk's
    row when it comes back byte-identical, precisely so a re-ingest stops destroying the
    note's mentions and its published wiki citations. The in-place re-anchor these tests
    cover is still load-bearing for the cases carry-over declines, and a rewrite is one.
    """
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET body = :b WHERE id = :n"), {"b": body, "n": note_id}
        )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})


async def _fact_chunk(maker: async_sessionmaker[AsyncSession], note_id: str) -> str | None:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT chunk_id::text FROM app.facts WHERE note_id = :n"), {"n": note_id}
            )
        ).scalar_one()


async def test_refresh_after_reingest_re_anchors_the_citation(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A re-ingest (an edit, an OCR re-describe, an appended clarification) deletes
    every chunk of the note, and `facts.chunk_id` is ON DELETE SET NULL — so the
    note's facts lose their citation. Re-integration then lands on decide()'s
    refresh path, which must re-anchor: `wiki/builder.py` INNER JOINs chunks, so a
    fact left with a null chunk silently drops out of its article while the
    article rebuilds without it."""
    person = fresh_person()
    facts = [home_fact(person, "Golden", confidence=0.9)]
    note_id = await analyzed_note(
        maker, tmp_path, "Sarah moved to Golden.", extraction(person, facts)
    )
    assert await _fact_chunk(maker, note_id) is not None

    # A rewritten body: no chunk of the old generation survives, so the citation is
    # nulled. (An unchanged re-ingest now carries its chunks over instead.)
    await rewritten_reingest(maker, tmp_path, note_id, "Sarah has moved house again.")
    assert await _fact_chunk(maker, note_id) is None  # the bug's precondition

    await analyze(maker, note_id, extraction(person, facts))

    chunk_id = await _fact_chunk(maker, note_id)
    assert chunk_id is not None
    async with scoped_session(maker, OWNER) as s:
        # The re-anchored citation points at a LIVE chunk of this note (a dangling
        # id the FK would have refused, a null one the wiki silently drops).
        assert (
            await s.execute(
                text("SELECT note_id::text FROM app.chunks WHERE id = :c"), {"c": chunk_id}
            )
        ).scalar_one() == note_id
        entity_id = (
            await s.execute(
                text("SELECT entity_id FROM app.facts WHERE note_id = :n"), {"n": note_id}
            )
        ).scalar_one()

    # And the fact is still a citable claim: this is the builder's own join.
    builder = WikiBuilder(
        maker, embed=_NoEmbed(), rewriter=StubRewriter(), embedding_model="fake-embed"
    )
    async with scoped_session(maker, OWNER) as s:
        sourced = await builder._source(s, entity_id)
    assert sourced is not None
    assert [c.statement for c in sourced.claims] == [f"{person} moved to Golden."]


async def _fact_row(maker: async_sessionmaker[AsyncSession], note_id: str) -> Any:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT id::text AS id, status, valid_to, chunk_id::text AS chunk_id,"
                    " entity_id FROM app.facts WHERE note_id = :n"
                ),
                {"n": note_id},
            )
        ).one()


async def _chunk_owner(maker: async_sessionmaker[AsyncSession], chunk_id: str) -> str:
    """The note a chunk belongs to — a re-anchored citation must name THIS note's
    own live chunk (a dangling id the FK would have refused, a null one the wiki
    silently drops)."""
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT note_id::text FROM app.chunks WHERE id = :c"), {"c": chunk_id}
            )
        ).scalar_one()


async def test_interval_close_after_reingest_re_anchors_the_citation(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The in-place interval close is the second path a re-ingest strands. The note
    says "Sarah moved to Golden" (open); the owner edits it to supply the end date,
    the edit's re-ingest deletes every chunk (`facts.chunk_id` is ON DELETE SET NULL),
    and re-integration lands on decide()'s close branch — which closes THIS note's own
    open row. Without a re-anchor there the closed fact keeps a null chunk and drops
    out of its article: `wiki/builder.py` INNER JOINs chunks."""
    person = fresh_person()
    note_id = await analyzed_note(
        maker,
        tmp_path,
        "Sarah moved to Golden in June.",
        extraction(person, [home_fact(person, "Golden", confidence=0.9)]),
    )
    before = await _fact_row(maker, note_id)
    assert before.chunk_id is not None and before.valid_to is None

    # The owner's edit, as a rewrite: no chunk of the old generation survives, so the
    # citation is nulled and re-integration has to re-anchor it.
    await rewritten_reingest(maker, tmp_path, note_id, "Sarah lived in Golden until August.")
    assert await _fact_chunk(maker, note_id) is None  # the bug's precondition

    closing = home_fact(
        person,
        "Golden",
        confidence=0.9,
        end="2026-08-01T00:00:00-06:00",
        statement=f"{person} lived in Golden until August.",
    )
    await analyze(maker, note_id, extraction(person, [closing]))

    after = await _fact_row(maker, note_id)
    # One row, closed in place: the close branch, not a refresh (which never writes
    # valid_to) and not an insert (which would mint a second id).
    assert after.id == before.id and after.valid_to is not None
    assert after.chunk_id is not None
    assert await _chunk_owner(maker, after.chunk_id) == note_id

    # And the closed fact is still a citable claim: this is the builder's own join.
    builder = WikiBuilder(
        maker, embed=_NoEmbed(), rewriter=StubRewriter(), embedding_model="fake-embed"
    )
    async with scoped_session(maker, OWNER) as s:
        sourced = await builder._source(s, after.entity_id)
    assert sourced is not None
    assert [c.statement for c in sourced.claims] == [closing["statement"]]


async def _seed_person(maker: async_sessionmaker[AsyncSession], name: str) -> str:
    """A confirmed entity the held intent below resolves to BY ID: mode="new" mints a
    fresh entity every run, which would move the identity key the held row's
    idempotent refresh is looked up by (and so never exercise that refresh)."""
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text(
                        "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                        " VALUES (gen_random_uuid(), 'Person', :n, 'confirmed', 'general')"
                        " RETURNING id"
                    ),
                    {"n": name},
                )
            ).scalar_one()
        )


def held_intent(person: str, entity_id: str, fact: dict[str, Any]) -> IntegrationIntent:
    """One CROSS-SUBJECT fact, as an intent. A cross-subject link is force-staged by the
    arbiter (N3: never silently committed), so the fact bypasses decide() entirely and is
    written by `_insert_held_fact` as a pending_review row.

    Built as the object rather than as `integrate.note` JSON: R4 deleted the parser that
    turned one into the other with the model that emitted it, and the driver takes an
    intent directly now."""
    temporal = fact["temporal"]
    return IntegrationIntent(
        note_id="",  # the driver stamps the real one
        schema_version=1,
        prompt_version="test",
        integrator_version="test",
        entity_resolutions=[
            EntityResolution(
                mention_ref=person,
                mode="existing",
                proposed_entity_id=entity_id,
                attested_span=AttestedSpan("", "Sarah"),
                cross_subject=True,
            )
        ],
        facts=[
            IntentFact(
                entity_ref=person,
                predicate=fact["predicate"],
                qualifier=fact["qualifier"],
                kind=fact["kind"],
                statement=fact["statement"],
                value_json=fact["value_json"],
                assertion=fact["assertion"],
                object_entity_ref=None,
                temporal=(
                    None
                    if temporal is None
                    else IntentTemporal(
                        phrase=temporal.get("phrase"),
                        resolved_start=parse_datetime(temporal.get("resolved_start")),
                        resolved_end=parse_datetime(temporal.get("resolved_end")),
                        precision=str(temporal.get("precision") or "unknown"),
                    )
                ),
                attested_span=AttestedSpan("", "Sarah"),
                self_confidence=fact["confidence"],
                inferred=False,
            )
        ],
    )


async def test_held_fact_refresh_after_reingest_re_anchors_the_citation(
    maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The third path a re-ingest strands: an arbiter-HELD fact. `_insert_held_fact`
    refreshes this note's existing pending_review row in place (so the open card's
    fact_id link survives) instead of churning a fresh id — and that in-place update
    must re-anchor the citation the re-ingest nulled. A held fact is not published
    while it is held, but accepting its card pins it ACTIVE, and it would then join
    the article with no chunk to cite — silently dropped by the builder's join."""
    person = fresh_person()
    entity_id = await _seed_person(maker, person)
    fact = home_fact(person, "Golden", confidence=0.9)
    intent = held_intent(person, entity_id, fact)

    # Ingested then integrated under the held intent only: `analyzed_note`'s default
    # intent would commit the fact ACTIVE, and the held row would then be a second
    # row beside it rather than the note's one fact.
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER,
        client_id=f"held-{uuid.uuid4()}",
        domain="general",
        destination=None,
        body="Sarah moved to Golden in June.",
    )
    note_id = note.id
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})
    await analyze(maker, note_id, extraction(person, [fact]), intent)
    before = await _fact_row(maker, note_id)
    assert before.status == "pending_review" and before.chunk_id is not None

    await rewritten_reingest(maker, tmp_path, note_id, "Sarah has moved house again.")
    assert await _fact_chunk(maker, note_id) is None  # the bug's precondition

    await analyze(maker, note_id, extraction(person, [fact]), intent)

    after = await _fact_row(maker, note_id)
    # The same held row, refreshed in place (a churned id would orphan its card).
    assert after.id == before.id and after.status == "pending_review"
    assert after.chunk_id is not None
    assert await _chunk_owner(maker, after.chunk_id) == note_id
