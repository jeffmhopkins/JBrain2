"""Owner correction notes end-to-end (Phase 6 Wave A+) against real Postgres: an
`owner_correction` note out-argues the graph — its fact commits active + pinned and
force-supersedes the prior active head on the same (entity, predicate, qualifier).
"""

import json
import uuid

import pytest
from sqlalchemy import select, text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Fact
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _pipeline(maker, entity_id: str):  # noqa: F811
    # The correction note asserts Globex is in tech; the intent resolves Globex to the
    # EXISTING entity so the correction fact lands on the same address as the seeded fact.
    extract = json.dumps(
        {
            "title": "Correction",
            "tags": ["work"],
            "mentions": [{"name": "Globex", "kind": "Organization", "surface_text": "Globex"}],
            "facts": [
                {
                    "entity_ref": "Globex",
                    "predicate": "industry",
                    "qualifier": "",
                    "kind": "attribute",
                    "statement": "Globex is in tech",
                    "value_json": None,
                    "assertion": "asserted",
                    "object_entity_ref": None,
                    "domain": "general",
                    "temporal": None,
                }
            ],
            "temporal_tokens": [],
        }
    )
    intent = json.dumps(
        {
            "resolutions": [
                {
                    "mention_ref": "Globex",
                    "mode": "existing",
                    "entity_id": entity_id,
                    "attested_span": {"chunk_id": "x", "surface": "Globex"},
                }
            ],
            "facts": [
                {
                    "entity_ref": "Globex",
                    "predicate": "industry",
                    "kind": "attribute",
                    "assertion": "asserted",
                    "statement": "Globex is in tech",
                    "self_confidence": 0.95,
                    "chunk_id": "x",
                    "surface": "tech",
                }
            ],
        }
    )
    fake = FakeLlmClient(responses=[extract, intent])
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    return AnalysisPipeline(maker, router)


async def test_owner_correction_supersedes_and_pins(maker, tmp_path):  # noqa: F811
    # Seed an existing active fact: Globex industry = retail.
    eid = str(uuid.uuid4())
    old_fact = str(uuid.uuid4())
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                " VALUES (:i, 'Organization', 'Globex', 'confirmed', 'general')"
            ),
            {"i": eid},
        )
        await session.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, qualifier, kind, statement,"
                " assertion, reported_at, note_id, extractor, prompt_version, confidence,"
                " domain_code, status)"
                " VALUES (:i, :e, 'industry', '', 'attribute', 'Globex is in retail', 'asserted',"
                " '2026-01-01T00:00:00Z', :n, 'seed', 'v1', 1.0, 'general', 'active')"
            ),
            # A standalone seed note for the FK (its own id; not the correction note).
            {"i": old_fact, "e": eid, "n": await _seed_note(session)},
        )

    # An owner correction note asserting the right value.
    note_id = await make_note(maker, domain="general", body="Globex is actually in tech now.")
    await ingest(maker, note_id, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text("UPDATE app.notes SET provenance = 'owner_correction' WHERE id = :n"),
            {"n": note_id},
        )

    await _pipeline(maker, eid).integrate_note({"note_id": note_id})

    async with scoped_session(maker, SYSTEM_CTX) as session:
        rows = (
            (
                await session.execute(
                    select(Fact).where(
                        Fact.entity_id == uuid.UUID(eid), Fact.predicate == "industry"
                    )
                )
            )
            .scalars()
            .all()
        )
    by_id = {str(f.id): f for f in rows}
    old = by_id[old_fact]
    new = next(f for f in rows if str(f.id) != old_fact)
    # The seeded retail fact is superseded by the correction; the correction is active + pinned.
    assert old.status == "superseded"
    assert str(old.superseded_by) == str(new.id)
    assert new.status == "active"
    assert new.pinned is True
    assert "tech" in new.statement


async def _seed_note(session) -> str:
    nid = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO app.notes (id, client_id, domain_code, body, ingest_state,"
            " integration_state) VALUES (:i, :c, 'general', 'seed', 'indexed', 'integrated')"
        ),
        {"i": nid, "c": nid[:12]},
    )
    return nid
