"""Migration 0007 RLS proofs and the analyze_note pipeline end to end against
real Postgres, with the LLM faked (scripted note.extract responses). Also
exercises the analysis read API and the review resolve endpoint through the
real FastAPI app."""

import json
import uuid
from collections.abc import AsyncIterator
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
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.main import create_app
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import PermanentJobError
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


async def make_note(maker: async_sessionmaker[AsyncSession], *, domain: str, body: str) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"ana-{uuid.uuid4()}", domain=domain, destination=None, body=body
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

    await analyzer(maker, [json.dumps(extraction_payload())]).analyze_note({"note_id": note_id})

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
    assert any(
        r.payload.get("fact_a") == str(old.id) and r.payload.get("fact_b") == str(new.id)
        for r in reviews
    )


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
            assert fact_shape["source_snippet"]  # cites its chunk text

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
            assert client.get(f"/api/entities/{uuid.uuid4()}").status_code == 404

            # --- review inbox: collision item is open, oldest first.
            items = client.get("/api/review", params={"status": "open"}).json()["items"]
            collision = next(i for i in items if i["kind"] == "attribute_collision")
            assert set(collision) == {"id", "kind", "payload", "domain", "created_at"}
            fact_a, fact_b = collision["payload"]["fact_a"], collision["payload"]["fact_b"]

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

            # --- ops usage card: tokens landed, grok-4.3 is priced.
            usage = client.get("/api/ops/llm-usage").json()
            assert set(usage) == {"today", "month", "by_task", "days"}
            assert usage["today"]["input_tokens"] >= 1
            assert usage["today"]["cost_usd"] is not None
            assert any(t["task"] == "note.extract" for t in usage["by_task"])
            assert usage["days"]
    finally:
        await engine.dispose()
