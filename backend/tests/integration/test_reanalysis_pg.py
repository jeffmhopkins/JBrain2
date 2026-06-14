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

from jbrain.db.session import scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def fresh_person() -> str:
    """A per-test entity name: the suite shares one database, and a reused
    name would resolve to an earlier test's entity and upsert ITS facts."""
    return f"Sarah {uuid.uuid4().hex[:8]}"


def home_fact(person: str, city: str, *, confidence: float) -> dict[str, Any]:
    return {
        "predicate": "homeLocation",
        "qualifier": "",
        "kind": "state",
        "statement": f"{person} moved to {city}.",
        "value_json": {"city": city},
        "assertion": "asserted",
        "entity_ref": person,
        "object_entity_ref": None,
        "temporal": {
            "phrase": "",
            "resolved_start": "2026-06-10T00:00:00-06:00",
            "resolved_end": None,
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
    maker: async_sessionmaker[AsyncSession], note_id: str, extraction_json: str
) -> None:
    # Drive integrate_note through the shared driver: it parses this scripted
    # extraction and commits via a name-match default intent, so re-running with
    # a different extraction exercises the genuine retraction/supersession sweep.
    from tests.integration.test_extraction_pg import analyzer

    await analyzer(maker, [extraction_json]).analyze_note({"note_id": note_id})


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
                    "SELECT status, payload FROM app.review_items WHERE kind = :kind"
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
    chain link + an open fact_conflict card)."""
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
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.review_items SET status = 'resolved', resolved_at = now()"
                " WHERE kind = 'fact_conflict' AND payload->>'note_id' = :nid"
            ),
            {"nid": note_b},
        )

    await analyze(maker, note_b, extraction(person, []))

    facts = {f["city"]: f for f in await fact_rows(maker, note_a, note_b)}
    assert facts["Boulder"]["status"] == "retracted"
    assert facts["Denver"]["status"] == "active"
    # The human's decision survives the re-run, frozen snippets and all.
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
        # Stale: the re-extraction below no longer references "Alex".
        ("open", "Alex", note_id),
        # Still referenced: must survive.
        ("open", person, note_id),
        # Human history: never touched even though stale.
        ("dismissed", "Alex", note_id),
        # Another note's card: out of this run's scope.
        ("open", "Alex", other_note),
    ]
    async with scoped_session(maker, OWNER) as s:
        for status, name, nid in cards:
            await s.execute(
                text(
                    "INSERT INTO app.review_items (id, kind, payload, status, domain_code)"
                    " VALUES (gen_random_uuid(), 'ambiguous_mention',"
                    " cast(:payload AS jsonb), :status, 'general')"
                ),
                {"payload": json.dumps({"name": name, "note_id": nid}), "status": status},
            )

    await analyze(maker, note_id, extraction(person, []))

    remaining = {
        (r["status"], r["payload"]["name"], r["payload"]["note_id"])
        for r in await review_rows(maker, "ambiguous_mention", note_id, other_note)
    }
    assert remaining == {
        ("open", person, note_id),
        ("dismissed", "Alex", note_id),
        ("open", "Alex", other_note),
    }
