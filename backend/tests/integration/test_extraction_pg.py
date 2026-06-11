"""Migration 0007 RLS proofs and the analyze_note pipeline end to end against
real Postgres, with the LLM faked (scripted note.extract responses). Also
exercises the analysis read API and the review resolve endpoint through the
real FastAPI app."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.main import create_app
from jbrain.models.analysis import Entity, Fact
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.storage import FsBlobStore
from jbrain.usage import SqlUsageRecorder
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_note(
    maker: async_sessionmaker[AsyncSession],
    *,
    domain: str,
    body: str,
    created_at: datetime | None = None,
    tz_offset: int | None = None,
) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER,
        client_id=f"ana-{uuid.uuid4()}",
        domain=domain,
        destination=None,
        body=body,
        created_at=created_at,
        tz_offset_minutes=tz_offset,
    )
    return note.id


async def ingest(maker: async_sessionmaker[AsyncSession], note_id: str, tmp_path: Any) -> None:
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})


def analyzer(maker: async_sessionmaker[AsyncSession], responses: list[str]) -> AnalysisPipeline:
    fake = FakeLlmClient(responses)
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3")},
        recorder=SqlUsageRecorder(maker),
    )
    return AnalysisPipeline(maker, router)


async def rows(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    sql: str,
    **params: Any,
) -> list[Any]:
    async with scoped_session(maker, ctx) as session:
        return list((await session.execute(text(sql), params)).all())


def extraction_payload(**overrides: Any) -> dict[str, Any]:
    """The scripted checkup-note extraction the fake model returns."""
    payload: dict[str, Any] = {
        "title": "Morning checkup with Dr. Patel",
        "tags": ["health", "checkup", "blood pressure"],
        "mentions": [
            {"name": "Me", "kind": "Person", "surface_text": "My"},
            {"name": "Dr. Patel", "kind": "Person", "surface_text": "Dr. Patel"},
        ],
        "facts": [
            {
                "predicate": "bloodPressure",
                "qualifier": "",
                "kind": "measurement",
                "statement": "Blood pressure was 118/76 this morning.",
                "value_json": {"systolic": 118, "diastolic": 76, "unit": "mmHg"},
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": {
                    "phrase": "this morning",
                    "resolved_start": "2026-06-10T08:00:00+00:00",
                    "resolved_end": None,
                    "precision": "day",
                },
                "domain": "health",
                "confidence": 0.95,
            },
            {
                "predicate": "address",
                "qualifier": "",
                "kind": "state",
                "statement": "Lives at 99 Pine Ave.",
                "value_json": {"street": "99 Pine Ave"},
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": {
                    "phrase": "last week",
                    "resolved_start": "2026-06-03T00:00:00+00:00",
                    "resolved_end": None,
                    "precision": "day",
                },
                "domain": "general",
                "confidence": 0.8,
            },
        ],
        "temporal_tokens": [
            {
                "phrase": "this morning",
                "kind": "point",
                "resolved_start": "2026-06-10T08:00:00+00:00",
                "resolved_end": None,
                "precision": "day",
                "rrule": None,
            },
            {
                "phrase": "last week",
                "kind": "point",
                "resolved_start": "2026-06-03T00:00:00+00:00",
                "resolved_end": None,
                "precision": "day",
                "rrule": None,
            },
        ],
    }
    payload.update(overrides)
    return payload


CHECKUP_BODY = "Saw Dr. Patel this morning. My BP was 118/76. We moved to 99 Pine Ave last week."


# --- RLS isolation (CLAUDE.md rule 3) ----------------------------------------


async def test_note_analysis_enforces_domain_firewall(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    note_id = await make_note(maker, domain="health", body="BP 118/76")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.note_analysis (note_id, title, tags, domain_code)"
                " VALUES (:nid, 'BP reading', '{health}', 'health')"
            ),
            {"nid": note_id},
        )
    for ctx, expected in ((HEALTH_ONLY, 1), (OWNER, 1), (GENERAL_ONLY, 0), (UNSCOPED, 0)):
        seen = await rows(
            maker, ctx, "SELECT 1 FROM app.note_analysis WHERE note_id = :nid", nid=note_id
        )
        assert len(seen) == expected, ctx
    # A scoped writer cannot smuggle a header into another domain.
    other = await make_note(maker, domain="general", body="plain")
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_analysis (note_id, domain_code) VALUES (:nid, 'health')"
                ),
                {"nid": other},
            )


async def test_llm_usage_is_owner_only(maker: async_sessionmaker[AsyncSession]) -> None:
    marker = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.llm_usage (id, task, provider, model, input_tokens,"
                " output_tokens) VALUES (:id, 'note.extract', 'xai', 'grok-4.3', 10, 5)"
            ),
            {"id": marker},
        )
    mine = await rows(maker, OWNER, "SELECT 1 FROM app.llm_usage WHERE id = :id", id=marker)
    assert len(mine) == 1
    # Telemetry is invisible to every non-owner principal kind, scopes or not.
    for ctx in (HEALTH_ONLY, GENERAL_ONLY, UNSCOPED):
        assert await rows(maker, ctx, "SELECT 1 FROM app.llm_usage WHERE id = :id", id=marker) == []
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, HEALTH_ONLY) as s:
            await s.execute(
                text(
                    "INSERT INTO app.llm_usage (id, task, provider, model, input_tokens,"
                    " output_tokens) VALUES (gen_random_uuid(), 't', 'p', 'm', 1, 1)"
                )
            )


# --- analyze_note end to end --------------------------------------------------


async def test_analyze_note_lands_everything(
    maker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    note_id = await make_note(maker, domain="general", body=CHECKUP_BODY)
    await ingest(maker, note_id, tmp_path)

    # Ingest enqueued the analysis job (alongside embed) — ingest is LLM-free.
    jobs = await rows(
        maker,
        OWNER,
        "SELECT kind FROM app.jobs WHERE payload->>'note_id' = :nid ORDER BY kind",
        nid=note_id,
    )
    assert "analyze_note" in {j.kind for j in jobs}

    # The API's lifecycle flag rides the note row as a correlated EXISTS:
    # false until the analysis header lands, true right after.
    note = await SqlNotesRepo(maker).get_note(OWNER, note_id)
    assert note is not None and note.analyzed is False

    await analyzer(maker, [json.dumps(extraction_payload())]).analyze_note({"note_id": note_id})

    note = await SqlNotesRepo(maker).get_note(OWNER, note_id)
    assert note is not None and note.analyzed is True

    header = (
        await rows(
            maker,
            OWNER,
            "SELECT title, tags, extractor, prompt_version, analyzed_at"
            " FROM app.note_analysis WHERE note_id = :nid",
            nid=note_id,
        )
    )[0]
    assert header.title == "Morning checkup with Dr. Patel"
    assert list(header.tags) == ["health", "checkup", "blood pressure"]
    assert header.extractor == "xai:grok-4.3"
    assert header.prompt_version and header.analyzed_at is not None

    # The Me entity exists once, hard-linked to a subject row.
    me = (
        await rows(
            maker,
            OWNER,
            "SELECT id, subject_id, status, kind FROM app.entities WHERE canonical_name = 'Me'",
        )
    )[0]
    assert me.subject_id is not None and me.status == "confirmed" and me.kind == "Person"

    # Dr. Patel was created provisional with an alias and a span-anchored mention.
    patel = (
        await rows(
            maker,
            OWNER,
            "SELECT id, status FROM app.entities WHERE canonical_name = 'Dr. Patel'",
        )
    )[0]
    assert patel.status == "provisional"
    aliases = await rows(
        maker,
        OWNER,
        "SELECT alias_norm FROM app.entity_aliases WHERE entity_id = :eid",
        eid=str(patel.id),
    )
    assert [a.alias_norm for a in aliases] == ["dr. patel"]
    mentions = await rows(
        maker,
        OWNER,
        "SELECT entity_id, surface_text, char_start, char_end, link_method"
        " FROM app.entity_mentions WHERE note_id = :nid",
        nid=note_id,
    )
    assert {m.surface_text for m in mentions} == {"My", "Dr. Patel"}
    patel_mention = next(m for m in mentions if m.surface_text == "Dr. Patel")
    assert CHECKUP_BODY[patel_mention.char_start : patel_mention.char_end] == "Dr. Patel"

    facts = await rows(
        maker,
        OWNER,
        "SELECT predicate, kind, status, domain_code, subject_id, valid_from,"
        " temporal_token_id, temporal_precision, extractor, prompt_version, reported_at"
        " FROM app.facts WHERE note_id = :nid ORDER BY predicate",
        nid=note_id,
    )
    assert [f.predicate for f in facts] == ["address", "bloodPressure"]
    bp = facts[1]
    # Domain ratchet: a health fact in a general note ratchets UP, no review.
    assert bp.domain_code == "health"
    assert bp.subject_id == me.subject_id  # first person -> the owner subject
    assert bp.temporal_token_id is not None and bp.temporal_precision == "day"
    assert bp.valid_from is not None and bp.valid_from.isoformat().startswith("2026-06-10T08:00")
    assert bp.extractor == "xai:grok-4.3" and bp.prompt_version

    tokens = await rows(
        maker,
        OWNER,
        "SELECT surface_phrase, kind, capture_anchor FROM app.temporal_tokens"
        " WHERE note_id = :nid ORDER BY surface_phrase",
        nid=note_id,
    )
    assert [t.surface_phrase for t in tokens] == ["last week", "this morning"]
    # The anchor every phrase was resolved against is the capture time.
    assert all(t.capture_anchor is not None for t in tokens)

    # No review items for a clean first extraction (ratchet-up is free).
    assert await rows(maker, OWNER, "SELECT 1 FROM app.review_items WHERE status = 'open'") == []

    # Token accounting landed fire-and-forget.
    usage = await rows(
        maker,
        OWNER,
        "SELECT task, provider, model FROM app.llm_usage WHERE task = 'note.extract'",
    )
    assert usage and usage[0].provider == "xai" and usage[0].model == "grok-4.3"


async def test_backward_phrase_resolution_repaired_end_to_end(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Issue 3: a 07:13 capture whose 'last night' the model wrongly resolved to
    the capture day is repaired to the prior evening by the pipeline's local
    anchor — both the fact's valid_from and the temporal token land on Jun 10."""
    mst = timezone(timedelta(minutes=-360))
    note_id = await make_note(
        maker,
        domain="general",
        body="Jeff ate Celine's dinner last night.",
        created_at=datetime(2026, 6, 11, 13, 13, tzinfo=UTC),  # 07:13 local at -06:00
        tz_offset=-360,
    )
    payload = {
        "title": "Dinner",
        "tags": ["dinner", "food", "celine"],
        "mentions": [{"name": "Jeff", "kind": "Person", "surface_text": "Jeff"}],
        "facts": [
            {
                "predicate": "ate", "qualifier": "", "kind": "event",
                "statement": "Jeff ate Celine's dinner last night.", "value_json": None,
                "assertion": "asserted", "entity_ref": "Jeff", "object_entity_ref": None,
                "temporal": {
                    "phrase": "last night",
                    "resolved_start": "2026-06-11T20:00:00-06:00",  # capture day: wrong
                    "resolved_end": None, "precision": "day",
                },
                "domain": "general", "confidence": 0.7,
            }
        ],
        "temporal_tokens": [
            {
                "phrase": "last night", "kind": "point",
                "resolved_start": "2026-06-11T20:00:00-06:00",
                "resolved_end": None, "precision": "day", "rrule": None,
            }
        ],
    }  # fmt: skip
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note_id})

    facts = await rows(
        maker, OWNER, "SELECT valid_from FROM app.facts WHERE note_id = :nid", nid=note_id
    )
    assert len(facts) == 1 and facts[0].valid_from is not None
    assert facts[0].valid_from.astimezone(mst).date() == date(2026, 6, 10)

    tokens = await rows(
        maker,
        OWNER,
        "SELECT resolved_start FROM app.temporal_tokens WHERE note_id = :nid",
        nid=note_id,
    )
    assert len(tokens) == 1
    assert tokens[0].resolved_start.astimezone(mst).date() == date(2026, 6, 10)


async def test_backward_phrase_not_repaired_without_tz_offset(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Red-team Finding 1: with no client offset the anchor falls back to the
    stored UTC instant, whose date is the NEXT day for an evening capture. The
    pipeline must then withhold the anchor so a model-correct backward date is
    never clobbered. Capture = May 9 19:00 local (May 10 01:00Z); the model
    correctly put 'yesterday' on May 8 and it must survive untouched."""
    note_id = await make_note(
        maker,
        domain="general",
        body="We moved yesterday.",
        created_at=datetime(2026, 5, 10, 1, 0, tzinfo=UTC),  # 19:00 the prior day at -06:00
        tz_offset=None,
    )
    payload = {
        "title": "Move",
        "tags": ["move", "home", "address"],
        "mentions": [{"name": "Me", "kind": "Person", "surface_text": "We"}],
        "facts": [
            {
                "predicate": "relocated", "qualifier": "", "kind": "event",
                "statement": "Moved to 7 Birch Ln.", "value_json": {"place": "7 Birch Ln"},
                "assertion": "asserted", "entity_ref": "Me", "object_entity_ref": None,
                "temporal": {
                    "phrase": "yesterday",
                    "resolved_start": "2026-05-08T00:00:00-06:00",  # model-correct local day
                    "resolved_end": None, "precision": "day",
                },
                "domain": "general", "confidence": 0.9,
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note_id})
    facts = await rows(
        maker, OWNER, "SELECT valid_from FROM app.facts WHERE note_id = :nid", nid=note_id
    )
    assert len(facts) == 1 and facts[0].valid_from is not None
    # Preserved on May 8 — NOT shifted forward to the UTC-derived May 9.
    assert facts[0].valid_from.astimezone(timezone(timedelta(minutes=-360))).date() == date(
        2026, 5, 8
    )


async def test_reanalysis_is_idempotent(
    maker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    body = "Glucose was 95 this morning. Aunt Carol gets our mail at 7 Birch Ln."
    note_id = await make_note(maker, domain="general", body=body)
    await ingest(maker, note_id, tmp_path)
    # Predicates unique to this test so the shared module database can't
    # cross-match identity keys from other tests' facts.
    payload = extraction_payload(
        title="Glucose and mail",
        tags=["health", "mail", "family"],
        mentions=[
            {"name": "Me", "kind": "Person", "surface_text": "Glucose"},
            {"name": "Aunt Carol", "kind": "Person", "surface_text": "Aunt Carol"},
        ],
        facts=[
            {
                "predicate": "bloodGlucose",
                "qualifier": "",
                "kind": "measurement",
                "statement": "Blood glucose was 95 this morning.",
                "value_json": {"value": 95, "unit": "mg/dL"},
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": {
                    "phrase": "this morning",
                    "resolved_start": "2026-06-10T08:00:00+00:00",
                    "resolved_end": None,
                    "precision": "day",
                },
                "domain": "health",
                "confidence": 0.9,
            },
            {
                "predicate": "mailingAddress",
                "qualifier": "",
                "kind": "state",
                "statement": "Mail goes to 7 Birch Ln.",
                "value_json": {"street": "7 Birch Ln"},
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": None,
                "domain": "general",
                "confidence": 0.8,
            },
        ],
        temporal_tokens=[
            {
                "phrase": "this morning",
                "kind": "point",
                "resolved_start": "2026-06-10T08:00:00+00:00",
                "resolved_end": None,
                "precision": "day",
                "rrule": None,
            }
        ],
    )
    pipeline = analyzer(maker, [json.dumps(payload)])

    await pipeline.analyze_note({"note_id": note_id})
    counts_sql = (
        "SELECT (SELECT count(*) FROM app.facts WHERE note_id = :nid) AS facts,"
        " (SELECT count(*) FROM app.facts WHERE note_id = :nid AND status = 'active') AS active,"
        " (SELECT count(*) FROM app.entity_mentions WHERE note_id = :nid) AS mentions,"
        " (SELECT count(*) FROM app.temporal_tokens WHERE note_id = :nid) AS tokens,"
        " (SELECT count(*) FROM app.review_items WHERE status = 'open') AS reviews,"
        " (SELECT count(*) FROM app.entities WHERE canonical_name = 'Me') AS me"
    )
    first = (await rows(maker, OWNER, counts_sql, nid=note_id))[0]
    ids_sql = "SELECT id FROM app.facts WHERE note_id = :nid"
    fact_ids_before = {r.id for r in await rows(maker, OWNER, ids_sql, nid=note_id)}

    await pipeline.analyze_note({"note_id": note_id})
    second = (await rows(maker, OWNER, counts_sql, nid=note_id))[0]
    fact_ids_after = {r.id for r in await rows(maker, OWNER, ids_sql, nid=note_id)}

    assert tuple(first) == tuple(second)
    assert fact_ids_before == fact_ids_after  # upsert on the identity key, not re-insert


async def test_self_naming_fact_aliases_owner_so_later_bare_name_links_to_me(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """ "My full name is Jeffrey Mark Hopkins" teaches the resolver an alias on
    the owner; a SECOND note that names "Jeffrey Mark Hopkins" in the third
    person then lands the fact on the same Me entity instead of forking a new
    person (docs/ANALYSIS.md "Alias resolution & separation")."""
    naming_note = await make_note(
        maker, domain="general", body="My full name is Jeffrey Mark Hopkins."
    )
    naming_payload = {
        "title": "Full name",
        "tags": ["identity", "name", "self"],
        "mentions": [{"name": "Me", "kind": "Person", "surface_text": "My"}],
        "facts": [
            {
                "predicate": "fullName", "qualifier": "", "kind": "attribute",
                "statement": "My full name is Jeffrey Mark Hopkins.",
                "value_json": {"name": "Jeffrey Mark Hopkins"},
                "assertion": "asserted", "entity_ref": "Me", "object_entity_ref": None,
                "temporal": None, "domain": "general", "confidence": 0.97,
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip
    await analyzer(maker, [json.dumps(naming_payload)]).analyze_note({"note_id": naming_note})

    me = (await rows(maker, OWNER, "SELECT id FROM app.entities WHERE canonical_name = 'Me'"))[0]
    aliases = {
        a.alias_norm
        for a in await rows(
            maker,
            OWNER,
            "SELECT alias_norm FROM app.entity_aliases WHERE entity_id = :eid",
            eid=str(me.id),
        )
    }
    assert {"me", "jeffrey mark hopkins"} <= aliases

    # A later, third-person note naming the owner resolves onto Me, no new row.
    later_note = await make_note(maker, domain="general", body="Jeffrey Mark Hopkins turned 40.")
    later_payload = {
        "title": "Birthday",
        "tags": ["birthday", "milestone", "age"],
        "mentions": [
            {
                "name": "Jeffrey Mark Hopkins",
                "kind": "Person",
                "surface_text": "Jeffrey Mark Hopkins",
            }
        ],
        "facts": [
            {
                "predicate": "age", "qualifier": "", "kind": "attribute",
                "statement": "Jeffrey Mark Hopkins turned 40.",
                "value_json": {"value": 40, "unit": "year"},
                "assertion": "asserted", "entity_ref": "Jeffrey Mark Hopkins",
                "object_entity_ref": None, "temporal": None, "domain": "general",
                "confidence": 0.9,
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip
    await analyzer(maker, [json.dumps(later_payload)]).analyze_note({"note_id": later_note})

    # No forked entity for the declared name — the bare name resolved onto Me.
    forked = await rows(
        maker,
        OWNER,
        "SELECT 1 FROM app.entities WHERE canonical_name = 'Jeffrey Mark Hopkins'",
    )
    assert forked == []
    age_fact = await rows(
        maker,
        OWNER,
        "SELECT entity_id FROM app.facts WHERE predicate = 'age' AND note_id = :nid",
        nid=later_note,
    )
    assert age_fact and age_fact[0].entity_id == me.id


async def _seed_provisional_namesake(maker: async_sessionmaker[AsyncSession], name: str) -> str:
    """A prior note that minted `name` as its own provisional person — the
    entity a later self-naming declaration collides with. Kept on a unique
    name so the shared graph in this non-truncating file holds exactly one."""
    note = await make_note(maker, domain="general", body=f"{name} stopped by.")
    payload = {
        "title": "Visit",
        "tags": ["visit", "social", "note"],
        "mentions": [{"name": name, "kind": "Person", "surface_text": name}],
        "facts": [
            {
                "predicate": "visited", "qualifier": "", "kind": "event",
                "statement": f"{name} stopped by.", "value_json": None,
                "assertion": "asserted", "entity_ref": name, "object_entity_ref": None,
                "temporal": None, "domain": "general", "confidence": 0.9,
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note})
    rs = await rows(maker, OWNER, "SELECT id FROM app.entities WHERE canonical_name = :n", n=name)
    return str(rs[0].id)


def _declaration_payload(declarer: str, full_name: str) -> dict[str, Any]:
    """`declarer` states that their full name is `full_name` — a self-naming
    fact on a NON-owner entity, so the test never collides on the singleton
    Me.fullName (which would leak an attribute_collision into the shared graph)."""
    return {
        "title": "Full name",
        "tags": ["identity", "name", "person"],
        "mentions": [{"name": declarer, "kind": "Person", "surface_text": declarer}],
        "facts": [
            {
                "predicate": "fullName", "qualifier": "", "kind": "attribute",
                "statement": f"{declarer}'s full name is {full_name}.",
                "value_json": {"name": full_name},
                "assertion": "asserted", "entity_ref": declarer, "object_entity_ref": None,
                "temporal": None, "domain": "general", "confidence": 0.97,
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip


async def _open_merge_proposals(
    maker: async_sessionmaker[AsyncSession], a: str, b: str
) -> list[Any]:
    return await rows(
        maker,
        OWNER,
        "SELECT payload->>'entity_a' AS a, payload->>'entity_b' AS b FROM app.review_items"
        " WHERE kind = 'merge_proposal' AND status = 'open'"
        " AND payload->>'entity_a' IN (:x, :y) AND payload->>'entity_b' IN (:x, :y)",
        x=a,
        y=b,
    )


async def test_declared_name_collision_files_one_merge_proposal(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """When a self-naming fact's name already keys a DIFFERENT entity, the alias
    is NOT widened across both — the collision becomes a single merge_proposal
    (the older, more-anchored side as survivor), and re-analysis does not
    multiply it (docs/ANALYSIS.md "Alias resolution & separation")."""
    full_name = "Wilhelmina Garcia Okonkwo"
    declarer = "Mina O."
    namesake = await _seed_provisional_namesake(maker, full_name)
    declare = await make_note(
        maker, domain="general", body=f"{declarer}'s full name is {full_name}."
    )
    await analyzer(maker, [json.dumps(_declaration_payload(declarer, full_name))]).analyze_note(
        {"note_id": declare}
    )
    declarer_id = str(
        (
            await rows(
                maker, OWNER, "SELECT id FROM app.entities WHERE canonical_name = :n", n=declarer
            )
        )[0].id
    )

    proposals = await _open_merge_proposals(maker, namesake, declarer_id)
    assert len(proposals) == 1
    # entity_a is the survivor: the older namesake outranks the newer declarer.
    assert proposals[0].a == namesake and proposals[0].b == declarer_id
    # The name was NOT aliased onto the declarer — the merge decides identity.
    declarer_aliases = {
        r.alias_norm
        for r in await rows(
            maker,
            OWNER,
            "SELECT alias_norm FROM app.entity_aliases WHERE entity_id = :e",
            e=declarer_id,
        )
    }
    assert full_name.casefold() not in declarer_aliases

    # Re-analyzing the same declaration must not file a second card.
    await analyzer(maker, [json.dumps(_declaration_payload(declarer, full_name))]).analyze_note(
        {"note_id": declare}
    )
    assert len(await _open_merge_proposals(maker, namesake, declarer_id)) == 1


async def test_rejected_merge_is_never_re_proposed(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A distinct_from edge (a rejected merge) is honoured: a later note
    repeating the same self-naming collision files no new proposal."""
    full_name = "Anselm Beauregard Fitzwilliam"
    declarer = "Ansel B."
    namesake = await _seed_provisional_namesake(maker, full_name)
    first = await make_note(maker, domain="general", body=f"{declarer}'s full name is {full_name}.")
    await analyzer(maker, [json.dumps(_declaration_payload(declarer, full_name))]).analyze_note(
        {"note_id": first}
    )
    declarer_id = str(
        (
            await rows(
                maker, OWNER, "SELECT id FROM app.entities WHERE canonical_name = :n", n=declarer
            )
        )[0].id
    )
    assert len(await _open_merge_proposals(maker, namesake, declarer_id)) == 1

    # Simulate the human rejecting the merge: resolve the card + write the
    # permanent distinct_from edge the reject handler would.
    lo, hi = sorted((namesake, declarer_id))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.review_items SET status = 'resolved', resolved_at = now()"
                " WHERE kind = 'merge_proposal' AND status = 'open'"
                " AND payload->>'entity_a' IN (:x, :y) AND payload->>'entity_b' IN (:x, :y)"
            ),
            {"x": namesake, "y": declarer_id},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_distinctions (id, entity_a, entity_b, reason, domain_code)"
                " VALUES (gen_random_uuid(), :a, :b, 'merge rejected', 'general')"
            ),
            {"a": lo, "b": hi},
        )

    second = await make_note(
        maker, domain="general", body=f"{declarer}'s full name is {full_name}."
    )
    await analyzer(maker, [json.dumps(_declaration_payload(declarer, full_name))]).analyze_note(
        {"note_id": second}
    )
    # No new open card — the negative edge blocked the re-proposal.
    assert await _open_merge_proposals(maker, namesake, declarer_id) == []


async def test_reextraction_dropping_a_relationship_retracts_its_inverse(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A derived inverse is a shadow of its source: when a re-extraction no
    longer asserts the relationship and the sweep retracts the source, the
    reciprocal on the object's stream must retract too — never linger active."""
    note_id = await make_note(maker, domain="general", body="Quinn and Robin co-founded Lumen.")

    def mentions() -> list[dict[str, Any]]:
        return [
            {"name": "Quinn", "kind": "Person", "surface_text": "Quinn"},
            {"name": "Robin", "kind": "Person", "surface_text": "Robin"},
        ]

    with_edge = {
        "title": "Co-founders",
        "tags": ["company", "founders", "lumen"],
        "mentions": mentions(),
        "facts": [
            {
                "predicate": "cofounder", "qualifier": "", "kind": "relationship",
                "statement": "Quinn co-founded with Robin.", "value_json": None,
                "assertion": "asserted", "entity_ref": "Quinn", "object_entity_ref": "Robin",
                "temporal": None, "domain": "general", "confidence": 0.9,
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip
    # Re-extraction that no longer mentions the partnership at all.
    without_edge = {
        "title": "Co-founders",
        "tags": ["company", "founders", "lumen"],
        "mentions": mentions(),
        "facts": [],
        "temporal_tokens": [],
    }
    await analyzer(maker, [json.dumps(with_edge)]).analyze_note({"note_id": note_id})
    derived = await rows(
        maker,
        OWNER,
        "SELECT status FROM app.facts WHERE predicate = 'cofounder'"
        " AND derived_from_fact_id IS NOT NULL AND note_id = :nid",
        nid=note_id,
    )
    assert [r.status for r in derived] == ["active"]  # the reciprocal was materialized

    await analyzer(maker, [json.dumps(without_edge)]).analyze_note({"note_id": note_id})
    after = await rows(
        maker,
        OWNER,
        "SELECT status, derived_from_fact_id IS NOT NULL AS derived FROM app.facts"
        " WHERE predicate = 'cofounder' AND note_id = :nid",
        nid=note_id,
    )
    # Both the source and its shadow are retracted; neither lingers active.
    assert after and all(r.status == "retracted" for r in after)
    assert any(r.derived for r in after)


async def test_state_change_forms_supersession_chain(
    maker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    # Predicate unique to this test: the shared module database would
    # otherwise dedupe against other tests' identity keys.
    def residence_fact(street: str, start: str) -> dict[str, Any]:
        return {
            "predicate": "residence",
            "qualifier": "",
            "kind": "state",
            "statement": f"Lives at {street}.",
            "value_json": {"street": street},
            "assertion": "asserted",
            "entity_ref": "Me",
            "object_entity_ref": None,
            "temporal": {
                "phrase": "recently",
                "resolved_start": start,
                "resolved_end": None,
                "precision": "day",
            },
            "domain": "general",
            "confidence": 0.9,
        }

    def move_payload(street: str, start: str) -> str:
        return json.dumps(
            extraction_payload(
                title="Moving day",
                tags=["moving", "address", "home"],
                mentions=[{"name": "Me", "kind": "Person", "surface_text": "We"}],
                facts=[residence_fact(street, start)],
                temporal_tokens=[],
            )
        )

    first_note = await make_note(maker, domain="general", body="We moved to 4 Cedar Ct recently.")
    await ingest(maker, first_note, tmp_path)
    pipeline_one = analyzer(maker, [move_payload("4 Cedar Ct", "2026-06-01T00:00:00+00:00")])
    await pipeline_one.analyze_note({"note_id": first_note})

    second_note = await make_note(maker, domain="general", body="We moved to 12 Oak St yesterday.")
    await ingest(maker, second_note, tmp_path)
    pipeline_two = analyzer(maker, [move_payload("12 Oak St", "2026-06-09T00:00:00+00:00")])
    await pipeline_two.analyze_note({"note_id": second_note})

    chain = await rows(
        maker,
        OWNER,
        "SELECT id, status, superseded_by, valid_to, note_id FROM app.facts"
        " WHERE predicate = 'residence' ORDER BY created_at",
    )
    assert len(chain) == 2
    old, new = chain
    assert old.status == "superseded" and new.status == "active"
    assert old.superseded_by == new.id  # the chain IS the revision history
    assert old.valid_to is not None  # SCD-2 close at the new interval's start
    assert str(old.note_id) == first_note and str(new.note_id) == second_note

    reviews = await rows(
        maker,
        OWNER,
        "SELECT payload FROM app.review_items WHERE kind = 'fact_conflict' AND status = 'open'",
    )
    item = next(
        r.payload
        for r in reviews
        if r.payload.get("fact_a") == str(old.id) and r.payload.get("fact_b") == str(new.id)
    )
    # The change-notice card: ids plus the display fields the UI renders.
    assert item["summary"] == "Me's residence changed"
    assert [c["action"] for c in item["choices"]] == ["accept_a", "accept_b"]
    assert item["choices"][0]["label"] == "Lives at 4 Cedar Ct."
    assert "<mark>We</mark>" in item["snippet"]


async def test_relocation_state_supersedes_across_notes(
    maker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    """Bug 1 regression: a relocation is a `state` on the canonical schema.org
    predicate homeLocation, so a later note's city supersedes the earlier one
    (SCD-2 close + fact_conflict review), not two forked events."""

    def home_fact(place: str, start: str) -> dict[str, Any]:
        return {
            "predicate": "homeLocation",
            "qualifier": "",
            "kind": "state",
            "statement": f"Sarah lives in {place}.",
            "value_json": {"place": place},
            "assertion": "asserted",
            "entity_ref": "Sarah",
            "object_entity_ref": None,
            "temporal": {
                "phrase": "now",
                "resolved_start": start,
                "resolved_end": None,
                "precision": "day",
            },
            "domain": "location",
            "confidence": 0.9,
        }

    def move_payload(place: str, start: str) -> str:
        return json.dumps(
            extraction_payload(
                title="Sarah's move",
                tags=["sarah", "location", "moving"],
                mentions=[{"name": "Sarah", "kind": "Person", "surface_text": "Sarah"}],
                facts=[home_fact(place, start)],
                temporal_tokens=[],
            )
        )

    note_a = await make_note(maker, domain="general", body="Sarah moved to Denver.")
    await ingest(maker, note_a, tmp_path)
    await analyzer(maker, [move_payload("Denver", "2026-06-10T00:00:00+00:00")]).analyze_note(
        {"note_id": note_a}
    )

    note_b = await make_note(maker, domain="general", body="Sarah actually moved to Boulder.")
    await ingest(maker, note_b, tmp_path)
    await analyzer(maker, [move_payload("Boulder", "2026-06-10T00:00:01+00:00")]).analyze_note(
        {"note_id": note_b}
    )

    chain = await rows(
        maker,
        OWNER,
        "SELECT id, status, superseded_by, valid_to, statement, note_id FROM app.facts"
        " WHERE predicate = 'homeLocation' ORDER BY created_at",
    )
    assert len(chain) == 2
    denver, boulder = chain
    assert denver.statement == "Sarah lives in Denver." and denver.status == "superseded"
    assert boulder.statement == "Sarah lives in Boulder." and boulder.status == "active"
    assert denver.superseded_by == boulder.id  # Boulder-current / Denver-superseded rail
    assert denver.valid_to is not None  # SCD-2 close
    assert str(denver.note_id) == note_a and str(boulder.note_id) == note_b

    review = (
        await rows(
            maker,
            OWNER,
            "SELECT payload FROM app.review_items WHERE kind = 'fact_conflict'"
            " AND status = 'open' AND payload->>'predicate' = 'homeLocation'",
        )
    )[0].payload
    assert review["fact_a"] == str(denver.id) and review["fact_b"] == str(boulder.id)


async def test_malformed_extraction_is_permanent_and_writes_nothing(
    maker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    note_id = await make_note(maker, domain="general", body="short note")
    await ingest(maker, note_id, tmp_path)
    # Schema-shaped JSON that fails structural validation (missing facts/tags).
    pipeline = analyzer(maker, ['{"title": "x"}'])
    with pytest.raises(PermanentJobError):
        await pipeline.analyze_note({"note_id": note_id})
    assert (
        await rows(
            maker, OWNER, "SELECT 1 FROM app.note_analysis WHERE note_id = :nid", nid=note_id
        )
        == []
    )
    assert (
        await rows(maker, OWNER, "SELECT 1 FROM app.facts WHERE note_id = :nid", nid=note_id) == []
    )


async def test_missing_note_is_a_noop(maker: async_sessionmaker[AsyncSession]) -> None:
    await analyzer(maker, ["{}"]).analyze_note({"note_id": str(uuid.uuid4())})


async def test_domain_promotion_review_carries_display_fields(
    maker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    # A general fact inside a health note ratchets DOWN -> promotion review.
    note_id = await make_note(maker, domain="health", body="Asked Dr. Akin to fax the form.")
    await ingest(maker, note_id, tmp_path)
    payload = extraction_payload(
        title="Fax request",
        tags=["paperwork"],
        mentions=[{"name": "Dr. Akin", "kind": "Person", "surface_text": "Dr. Akin"}],
        facts=[
            {
                "predicate": "faxRequest",
                "qualifier": "",
                "kind": "event",
                "statement": "Asked Dr. Akin's office to fax the form.",
                "value_json": None,
                "assertion": "asserted",
                "entity_ref": "Dr. Akin",
                "object_entity_ref": None,
                "temporal": None,
                "domain": "general",
                "confidence": 0.9,
            }
        ],
        temporal_tokens=[],
    )
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note_id})

    item = (
        await rows(
            maker,
            OWNER,
            "SELECT payload FROM app.review_items"
            " WHERE kind = 'domain_promotion' AND payload->>'note_id' = :nid",
            nid=note_id,
        )
    )[0].payload
    assert item["proposed_domain"] == "general" and item["note_domain"] == "health"
    assert item["summary"] == "this faxRequest fact may belong in general, not health"
    # The advertised verbs are exactly the actions resolve accepts here.
    assert set(item["outcomes"]) == {"accept", "reject"}
    assert "<mark>Dr. Akin</mark>" in item["snippet"]


async def test_ambiguous_mention_review_and_reject_dismissal(
    maker: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    # Two pre-existing entities answer to "Sam" -> layer 1 cannot pick.
    async with scoped_session(maker, OWNER) as s:
        for n in ("one", "two"):
            await s.execute(
                text(
                    "WITH e AS (INSERT INTO app.entities (id, kind, canonical_name, status,"
                    " domain_code) VALUES (gen_random_uuid(), 'Person', :name, 'provisional',"
                    " 'general') RETURNING id)"
                    " INSERT INTO app.entity_aliases (id, entity_id, alias, alias_norm,"
                    " domain_code) SELECT gen_random_uuid(), id, 'Sam', 'sam', 'general' FROM e"
                ),
                {"name": f"Sam {n}"},
            )
    note_id = await make_note(maker, domain="general", body="Sam said the quote covers it.")
    await ingest(maker, note_id, tmp_path)
    payload = extraction_payload(
        title="Roof quote",
        tags=["house"],
        mentions=[{"name": "Sam", "kind": "Person", "surface_text": "Sam"}],
        facts=[],
        temporal_tokens=[],
    )
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note_id})

    repo = SqlAnalysisRepo(maker)
    items = await repo.list_review(OWNER, "open")
    item = next(
        i for i in items if i["kind"] == "ambiguous_mention" and i["payload"]["note_id"] == note_id
    )
    assert item["payload"]["summary"] == "which Sam?"
    assert len(item["payload"]["entity_ids"]) == 2
    assert "<mark>Sam</mark>" in item["payload"]["snippet"]
    # The card advertises reject only; accepting a link needs layer 2/3.
    assert set(item["payload"]["outcomes"]) == {"reject"}

    resolved = await repo.resolve_review(OWNER, item["id"], "reject", {})
    assert resolved is not None and resolved["status"] == "dismissed"


# --- API round trip -----------------------------------------------------------


async def test_analysis_and_review_api_round_trip(
    database_url: str,  # noqa: F811
    tmp_path: Any,
) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Two notes asserting conflicting birthdays for Mom -> collision.
        def birthday_fact(date: str, statement: str) -> dict[str, Any]:
            return {
                "predicate": "birthDate",
                "qualifier": "",
                "kind": "attribute",
                "statement": statement,
                "value_json": {"date": date},
                "assertion": "asserted",
                "entity_ref": "Mom",
                "object_entity_ref": None,
                "temporal": None,
                "domain": "general",
                "confidence": 0.9,
            }

        def payload_for(date: str, statement: str) -> str:
            return json.dumps(
                extraction_payload(
                    title="About Mom",
                    tags=["family", "mom", "birthday"],
                    mentions=[{"name": "Mom", "kind": "Person", "surface_text": "Mom"}],
                    facts=[birthday_fact(date, statement)],
                    temporal_tokens=[],
                )
            )

        note_a = await make_note(maker, domain="general", body="Mom was born April 2, 1958.")
        await ingest(maker, note_a, tmp_path)
        pipeline_a = analyzer(maker, [payload_for("1958-04-02", "Mom was born on 1958-04-02.")])
        await pipeline_a.analyze_note({"note_id": note_a})
        note_b = await make_note(maker, domain="general", body="Mom's birthday is April 3, 1958.")
        await ingest(maker, note_b, tmp_path)
        pipeline_b = analyzer(maker, [payload_for("1958-04-03", "Mom was born on 1958-04-03.")])
        await pipeline_b.analyze_note({"note_id": note_b})

        # The owner key is minted against the same database the app serves.
        key = await service.rotate_owner_key(SqlAuthRepo(maker))
        settings = Settings(secure_cookies=False, database_url=database_url)
        app = create_app(settings)
        with TestClient(app) as client:
            login = client.post("/api/auth/session", json={"owner_key": key, "device_label": "it"})
            assert login.status_code == 204

            # --- note analysis view: the frozen frontend contract.
            view = client.get(f"/api/notes/{note_a}/analysis").json()
            assert set(view) == {
                "note_id",
                "title",
                "tags",
                "analyzed_at",
                "extractor",
                "facts",
                "entities",
                "temporal_tokens",
            }
            assert view["title"] == "About Mom"
            fact_shape = view["facts"][0]
            assert set(fact_shape) == {
                "id",
                "entity_id",
                "entity_name",
                "predicate",
                "qualifier",
                "kind",
                "statement",
                "value_json",
                "assertion",
                "status",
                "pinned",
                "confidence",
                "valid_from",
                "valid_to",
                "reported_at",
                "temporal_precision",
                "source_snippet",
            }
            assert fact_shape["entity_name"] == "Mom"
            # The cited span renders as a literal <mark>, like search snippets.
            assert "<mark>Mom</mark>" in fact_shape["source_snippet"]

            # Unknown note -> 404; un-analyzed note -> empty shell.
            assert client.get(f"/api/notes/{uuid.uuid4()}/analysis").status_code == 404
            bare = await make_note(maker, domain="general", body="not yet analyzed")
            shell = client.get(f"/api/notes/{bare}/analysis").json()
            assert shell["title"] is None and shell["facts"] == [] and shell["tags"] == []

            # --- entity view with current/history per predicate.
            mom_id = fact_shape["entity_id"]
            entity = client.get(f"/api/entities/{mom_id}").json()
            assert entity["canonical_name"] == "Mom"
            assert entity["aliases"] == ["Mom"]
            birth = next(p for p in entity["predicates"] if p["predicate"] == "birthDate")
            # Both collided facts are pending review: no current, full history.
            assert birth["current"] is None
            assert len(birth["history"]) == 2
            assert {f["status"] for f in birth["history"]} == {"pending_review"}
            assert len(entity["mentions"]) == 2
            assert all("<mark>Mom</mark>" in m["snippet"] for m in entity["mentions"])
            assert client.get(f"/api/entities/{uuid.uuid4()}").status_code == 404

            # --- review inbox: collision item is open, oldest first.
            items = client.get("/api/review", params={"status": "open"}).json()["items"]
            collision = next(i for i in items if i["kind"] == "attribute_collision")
            assert set(collision) == {
                "id",
                "kind",
                "payload",
                "status",
                "resolution",
                "domain",
                "created_at",
                "resolved_at",
            }
            assert collision["status"] == "open" and collision["resolution"] is None
            fact_a, fact_b = collision["payload"]["fact_a"], collision["payload"]["fact_b"]

            # History is newest-first and includes the (here pending) head.
            assert [f["id"] for f in birth["history"]] == [fact_b, fact_a]

            # Display fields ride alongside the ids; the advertised choice
            # actions are exactly what resolve accepts for this kind.
            payload = collision["payload"]
            assert payload["summary"] == "two values recorded for Mom's birthDate"
            assert "<mark>Mom</mark>" in payload["snippet"]
            assert [c["action"] for c in payload["choices"]] == ["accept_a", "accept_b"]
            assert payload["choices"][1]["label"] == "Mom was born on 1958-04-03."

            # Unknown action -> 400, untouched.
            bad = client.post(
                f"/api/review/{collision['id']}/resolve", json={"action": "frobnicate"}
            )
            assert bad.status_code == 400

            resolved = client.post(
                f"/api/review/{collision['id']}/resolve", json={"action": "accept_b"}
            )
            assert resolved.status_code == 200
            body = resolved.json()
            assert body["status"] == "resolved"
            assert body["resolution"]["action"] == "accept_b"

            # accept_b pinned the winner and retracted the loser.
            states = {
                r.id: (r.status, r.pinned)
                for r in await rows(
                    maker,
                    OWNER,
                    "SELECT id::text AS id, status, pinned FROM app.facts WHERE id IN (:a, :b)",
                    a=fact_a,
                    b=fact_b,
                )
            }
            assert states[fact_b] == ("active", True)
            assert states[fact_a] == ("retracted", False)

            # Resolving again conflicts; resolved items left the open queue.
            again = client.post(
                f"/api/review/{collision['id']}/resolve", json={"action": "accept_a"}
            )
            assert again.status_code == 409
            open_now = client.get("/api/review").json()["items"]
            assert collision["id"] not in {i["id"] for i in open_now}

            # The resolution recorded its graph effects with the prior state
            # a reopen needs to reverse them.
            effects = body["resolution"]["effects"]
            assert {e["action"] for e in effects} == {"pinned", "retracted"}
            pinned = next(e for e in effects if e["action"] == "pinned")
            assert pinned["fact_id"] == fact_b
            assert pinned["prior_status"] == "pending_review"
            retracted = next(e for e in effects if e["action"] == "retracted")
            assert retracted["fact_id"] == fact_a
            assert retracted["prior_status"] == "pending_review"

            # The decision shows in the resolved log, newest first.
            log = client.get("/api/review", params={"status": "resolved"}).json()["items"]
            entry = next(i for i in log if i["id"] == collision["id"])
            assert entry["status"] == "resolved" and entry["resolved_at"] is not None

            # Reopen = full unwind: both facts back to pending_review.
            reopened = client.post(f"/api/review/{collision['id']}/reopen")
            assert reopened.status_code == 200
            ro = reopened.json()
            assert ro["status"] == "open" and ro["resolved_at"] is None
            assert ro["resolution"]["reopened_at"] and ro["reopen_note"] is None
            states = {
                r.id: (r.status, r.pinned)
                for r in await rows(
                    maker,
                    OWNER,
                    "SELECT id::text AS id, status, pinned FROM app.facts WHERE id IN (:a, :b)",
                    a=fact_a,
                    b=fact_b,
                )
            }
            assert states[fact_b] == ("pending_review", False)
            assert states[fact_a] == ("pending_review", False)

            # Back in the open queue AND tombstoned in the resolved log.
            open_again = client.get("/api/review").json()["items"]
            assert collision["id"] in {i["id"] for i in open_again}
            tombs = client.get("/api/review", params={"status": "resolved"}).json()["items"]
            tomb = next(i for i in tombs if i["id"] == collision["id"])
            assert tomb["status"] == "open" and tomb["resolution"]["reopened_at"]

            # Reopening an open item conflicts.
            assert client.post(f"/api/review/{collision['id']}/reopen").status_code == 409

            # --- ops usage card: tokens landed, grok-4.3 is priced.
            usage = client.get("/api/ops/llm-usage").json()
            assert set(usage) == {"today", "month", "by_task", "days"}
            assert usage["today"]["input_tokens"] >= 1
            assert usage["today"]["cost_usd"] is not None
            assert any(t["task"] == "note.extract" for t in usage["by_task"])
            assert usage["days"]
    finally:
        await engine.dispose()


async def test_supersession_candidate_read_is_domain_and_object_scoped(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The candidate read behind decide(): the same graph address in another
    DOMAIN (C1 firewall) or pointing at another OBJECT (me.owns->Civic vs
    me.owns->kayak) must be invisible — except functional predicates, which
    keep one key across objects so a new employer still sees the old edge."""
    note_id = uuid.UUID(await make_note(maker, domain="general", body="candidate-read seed"))
    pipeline = analyzer(maker, [])
    now = datetime(2026, 6, 1, tzinfo=UTC)

    async with scoped_session(maker, SYSTEM_CTX) as session:
        me = Entity(kind="Person", canonical_name="CandidateReadMe", domain_code="general")
        civic = Entity(kind="Product", canonical_name="CR Civic", domain_code="general")
        kayak = Entity(kind="Product", canonical_name="CR Kayak", domain_code="general")
        acme = Entity(kind="Organization", canonical_name="CR Acme", domain_code="general")
        globex = Entity(kind="Organization", canonical_name="CR Globex", domain_code="general")
        session.add_all([me, civic, kayak, acme, globex])
        await session.flush()

        def edge(predicate: str, obj: uuid.UUID, domain: str, assertion: str) -> Fact:
            return Fact(
                entity_id=me.id,
                predicate=predicate,
                qualifier="",
                kind="state",
                statement=f"{predicate} edge",
                value_json=None,
                object_entity_id=obj,
                assertion=assertion,
                valid_from=now,
                reported_at=now,
                note_id=note_id,
                extractor="test",
                prompt_version="test",
                domain_code=domain,
            )

        civic_general = edge("ownsCandidateRead", civic.id, "general", "negated")
        session.add_all(
            [
                civic_general,
                edge("ownsCandidateRead", kayak.id, "general", "asserted"),
                edge("ownsCandidateRead", civic.id, "health", "asserted"),
                edge("employer", acme.id, "general", "asserted"),
                edge("employer", globex.id, "general", "asserted"),
                edge("employer", acme.id, "health", "asserted"),
            ]
        )
        await session.flush()

        owned = await pipeline._existing_facts(
            session, me.id, "ownsCandidateRead", "", None, civic.id, "general"
        )
        # The kayak edge and the health Civic edge are different facts.
        assert [f.id for f in owned] == [str(civic_general.id)]
        # The view carries assertion, so values_equal sees disposal flips.
        assert owned[0].assertion == "negated"

        # Functional predicate: one key across objects, still domain-scoped.
        employers = await pipeline._existing_facts(
            session, me.id, "employer", "", None, globex.id, "general"
        )
        assert {f.object_entity_id for f in employers} == {str(acme.id), str(globex.id)}


# --- mutual / inverse edges (Issue 2) ----------------------------------------


def _relationship_payload(predicate: str, obj_name: str, *, obj_kind: str = "Person") -> dict:
    """A one-fact relationship extraction: Me.<predicate> -> <obj>. Unique
    predicates per test keep the shared DB's global predicate queries clean."""
    return {
        "title": f"Me and {obj_name}",
        "tags": [obj_name.lower()],
        "mentions": [
            {"name": "Me", "kind": "Person", "surface_text": "I"},
            {"name": obj_name, "kind": obj_kind, "surface_text": obj_name},
        ],
        "facts": [
            {
                "predicate": predicate,
                "qualifier": "",
                "kind": "relationship",
                "statement": f"My {predicate} is {obj_name}.",
                "value_json": None,
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": obj_name,
                "temporal": {
                    "phrase": "",
                    "resolved_start": "2026-06-10T00:00:00+00:00",
                    "resolved_end": None,
                    "precision": "day",
                },
                "domain": "general",
                "confidence": 0.95,
            }
        ],
        "temporal_tokens": [],
    }


async def test_symmetric_inverse_edge_materialized_end_to_end(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The real pipeline materializes the reciprocal of a same-subject /
    null-subject relationship edge (Issue 2). Queries are scoped to THIS note
    so the shared DB's other spouse rows never leak in."""
    note_id = await make_note(maker, domain="general", body="I married Celine.")
    payload = _relationship_payload("spouse", "Celine")
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note_id})

    facts = await rows(
        maker,
        OWNER,
        "SELECT e.canonical_name AS entity, f.predicate, f.status,"
        " f.derived_from_fact_id, f.statement"
        " FROM app.facts f JOIN app.entities e ON e.id = f.entity_id"
        " WHERE f.predicate = 'spouse' AND f.note_id = :nid ORDER BY e.canonical_name",
        nid=note_id,
    )
    by_entity = {f.entity: f for f in facts}
    # The source edge (primary) and its derived reciprocal on Celine's stream.
    assert by_entity["Me"].derived_from_fact_id is None
    assert by_entity["Me"].status == "active"
    celine = by_entity["Celine"]
    assert celine.derived_from_fact_id is not None  # derived shadow
    assert celine.status == "active"
    assert "Me" in celine.statement


async def test_cross_subject_inverse_is_proposed_not_written(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """When the object entity is a DISTINCT security subject, the inverse is
    routed to the review inbox as an inverse_proposal and NEVER written onto
    that subject's stream (Issue 2, the firewall gate)."""
    note_id = await make_note(maker, domain="general", body="I have treated Patient X.")

    # Seed a confirmed object entity hard-linked to its OWN subject, resolvable
    # by exact alias — a different security subject than the owner ('Me').
    other_subject = str(uuid.uuid4())
    other_entity = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.subjects (id, kind, display_name) VALUES (:id, 'person', 'X')"),
            {"id": other_subject},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, subject_id, status,"
                " domain_code) VALUES (:id, 'Person', 'Patient X', :sub, 'confirmed', 'general')"
            ),
            {"id": other_entity, "sub": other_subject},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_aliases (id, entity_id, alias, alias_norm, domain_code)"
                " VALUES (gen_random_uuid(), :eid, 'Patient X', 'patient x', 'general')"
            ),
            {"eid": other_entity},
        )

    payload = _relationship_payload("hasTreated", "Patient X")
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note_id})

    # The directed edge exists on the owner's stream...
    source = await rows(
        maker,
        OWNER,
        "SELECT status FROM app.facts WHERE predicate = 'hasTreated' AND note_id = :nid",
        nid=note_id,
    )
    assert len(source) == 1 and source[0].status == "active"
    # ...but NO inverse was written onto the other subject's stream.
    inverse = await rows(
        maker,
        OWNER,
        "SELECT 1 FROM app.facts WHERE entity_id = :eid AND derived_from_fact_id IS NOT NULL",
        eid=other_entity,
    )
    assert inverse == []
    # The inverse is PROPOSED instead.
    proposals = await rows(
        maker,
        OWNER,
        "SELECT payload->>'subject' AS subject FROM app.review_items"
        " WHERE kind = 'inverse_proposal' AND payload->>'note_id' = :nid",
        nid=note_id,
    )
    assert len(proposals) == 1 and proposals[0].subject == "Patient X"


def _person_edge(subj: str, obj: str, predicate: str = "spouse") -> dict[str, Any]:
    return {
        "title": f"{subj} and {obj}",
        "tags": ["rel", subj.lower()],
        "mentions": [
            {"name": subj, "kind": "Person", "surface_text": subj},
            {"name": obj, "kind": "Person", "surface_text": obj},
        ],
        "facts": [
            {
                "predicate": predicate, "qualifier": "", "kind": "relationship",
                "statement": f"{subj} is married to {obj}.", "value_json": None,
                "assertion": "asserted", "entity_ref": subj, "object_entity_ref": obj,
                "temporal": None, "domain": "general", "confidence": 0.95,
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip


async def test_direct_assertion_promotes_a_derived_shadow_to_primary(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Red-team Finding 1: when a note DIRECTLY asserts what was only a derived
    shadow of another note's edge, the shadow is adopted as a primary fact owned
    by that note — so it survives deletion of the source that first reflected
    it, instead of silently riding (and dying with) the other note."""
    from jbrain.analysis.purge import purge_note_artifacts

    sql = (
        "SELECT f.derived_from_fact_id AS dff, f.note_id::text AS note FROM app.facts f"
        " JOIN app.entities e ON e.id = f.entity_id"
        " WHERE e.canonical_name = 'Roanen' AND f.predicate = 'spouse' AND f.status = 'active'"
    )
    note_a = await make_note(maker, domain="general", body="Quillon married Roanen.")
    await analyzer(maker, [json.dumps(_person_edge("Quillon", "Roanen"))]).analyze_note(
        {"note_id": note_a}
    )
    shadow = await rows(maker, OWNER, sql)
    assert len(shadow) == 1 and shadow[0].dff is not None and shadow[0].note == note_a

    note_b = await make_note(maker, domain="general", body="Roanen married Quillon.")
    await analyzer(maker, [json.dumps(_person_edge("Roanen", "Quillon"))]).analyze_note(
        {"note_id": note_b}
    )
    promoted = await rows(maker, OWNER, sql)
    assert len(promoted) == 1 and promoted[0].dff is None and promoted[0].note == note_b

    async with scoped_session(maker, SYSTEM_CTX) as s:
        await purge_note_artifacts(s, uuid.UUID(note_a))
        await s.commit()
    survivors = await rows(maker, OWNER, sql)
    assert len(survivors) == 1 and survivors[0].note == note_b


async def test_cross_subject_supersession_closes_shadow_without_cross_entity_link(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Red-team Finding 2: when a supersession's new object is a DISTINCT subject
    (gate fires, no inverse written), the old shadow closes with a NULL link —
    never a cross-entity pointer at the source fact on another entity."""
    shadow_sql = (
        "SELECT f.status, f.superseded_by FROM app.facts f"
        " JOIN app.entities e ON e.id = f.entity_id"
        " WHERE e.canonical_name = 'Celestina' AND f.predicate = 'spouse'"
    )
    note1 = await make_note(maker, domain="general", body="I married Celestina.")
    await analyzer(maker, [json.dumps(_relationship_payload("spouse", "Celestina"))]).analyze_note(
        {"note_id": note1}
    )
    assert (await rows(maker, OWNER, shadow_sql))[0].status == "active"

    other_subject, other_entity = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.subjects (id, kind, display_name) VALUES (:id, 'person', 'PatY')"
            ),
            {"id": other_subject},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, subject_id, status,"
                " domain_code) VALUES (:id, 'Person', 'Patient Y', :sub, 'confirmed', 'general')"
            ),
            {"id": other_entity, "sub": other_subject},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_aliases (id, entity_id, alias, alias_norm, domain_code)"
                " VALUES (gen_random_uuid(), :eid, 'Patient Y', 'patient y', 'general')"
            ),
            {"eid": other_entity},
        )
    note2 = await make_note(maker, domain="general", body="I married Patient Y.")
    await analyzer(maker, [json.dumps(_relationship_payload("spouse", "Patient Y"))]).analyze_note(
        {"note_id": note2}
    )
    closed = await rows(maker, OWNER, shadow_sql)
    assert len(closed) == 1 and closed[0].status == "superseded" and closed[0].superseded_by is None


async def test_conflict_resolution_cascades_to_derived_shadows(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Red-team Finding 3: resolving a spouse fact_conflict carries to the
    reciprocals — the kept side's shadow goes active, the dropped side's shadow
    retracts — and reopen reverses both, so a shadow never contradicts the
    human's verdict."""
    repo = SqlAnalysisRepo(maker)

    def shadow_status_sql(name: str) -> str:
        return (
            "SELECT f.status FROM app.facts f JOIN app.entities e ON e.id = f.entity_id"
            f" WHERE e.canonical_name = '{name}' AND f.predicate = 'spouse'"
            " AND f.derived_from_fact_id IS NOT NULL"
        )

    note1 = await make_note(maker, domain="general", body="I married Aldous.")
    await analyzer(maker, [json.dumps(_relationship_payload("spouse", "Aldous"))]).analyze_note(
        {"note_id": note1}
    )
    note2 = await make_note(maker, domain="general", body="I married Bettina.")
    await analyzer(maker, [json.dumps(_relationship_payload("spouse", "Bettina"))]).analyze_note(
        {"note_id": note2}
    )
    assert (await rows(maker, OWNER, shadow_status_sql("Aldous")))[0].status == "superseded"
    assert (await rows(maker, OWNER, shadow_status_sql("Bettina")))[0].status == "active"

    items = await rows(
        maker,
        OWNER,
        "SELECT id::text AS id FROM app.review_items WHERE kind = 'fact_conflict'"
        " AND status = 'open' AND payload->>'note_id' = :nid",
        nid=note2,
    )
    assert len(items) == 1
    await repo.resolve_review(OWNER, items[0].id, "accept_a", {})  # keep Aldous (the prior value)
    assert (await rows(maker, OWNER, shadow_status_sql("Aldous")))[0].status == "active"
    assert (await rows(maker, OWNER, shadow_status_sql("Bettina")))[0].status == "retracted"

    await repo.reopen_review(OWNER, items[0].id)
    assert (await rows(maker, OWNER, shadow_status_sql("Aldous")))[0].status == "superseded"
    assert (await rows(maker, OWNER, shadow_status_sql("Bettina")))[0].status == "active"


async def test_domain_floor_raises_clinical_fact_in_general_note(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Firewall hardening: a bloodPressure fact the model mislabeled `general`
    is floored to health by the deterministic predicate->domain map, so it lands
    behind the health RLS policy regardless of the model's per-fact judgment."""
    note_id = await make_note(maker, domain="general", body="BP was 120/80 this morning.")
    payload = {
        "title": "BP", "tags": ["bp", "reading", "vitals"],
        "mentions": [{"name": "Me", "kind": "Person", "surface_text": "BP"}],
        "facts": [
            {
                "predicate": "bloodPressure", "qualifier": "", "kind": "measurement",
                "statement": "BP was 120/80.", "value_json": {"systolic": 120, "diastolic": 80},
                "assertion": "asserted", "entity_ref": "Me", "object_entity_ref": None,
                "temporal": None, "domain": "general", "confidence": 0.9,  # model mislabels it
            }
        ],
        "temporal_tokens": [],
    }  # fmt: skip
    await analyzer(maker, [json.dumps(payload)]).analyze_note({"note_id": note_id})
    facts = await rows(
        maker,
        OWNER,
        "SELECT domain_code FROM app.facts WHERE note_id = :nid AND predicate = 'bloodPressure'",
        nid=note_id,
    )
    assert len(facts) == 1 and facts[0].domain_code == "health"
