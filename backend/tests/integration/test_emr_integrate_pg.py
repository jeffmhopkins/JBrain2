"""End-to-end deterministic EMR integration against real Postgres: the Epic
fixture parses, lowers, and commits through the SHIPPED arbiter
(plan_intent -> apply_intent) into health-domain graph facts
(docs/plans/EMR_IMPORT_PLAN.md §6.6). The LLM is never called. Proves the
importer's IntegrationIntents mint the expected entities and that `fhir_status`
threads all the way into `decide` — the fixture's `corrected` platelet has no
prior reading at its draw, so it is held for review (the §3.5 red-team outcome),
which only the status-aware transition produces.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.emr.epic import parse_epic
from jbrain.ingest.emr.integrate import integrate_parse_result
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact, ReviewItem
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_apply_intent_pg import _load_chunks
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "epic_report.txt"


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(maker, router)


async def _integrate(maker, tmp_path) -> str:  # noqa: F811
    note_id = await make_note(maker, domain="health", body="Imported EMR records.")
    await ingest(maker, note_id, tmp_path)
    chunks = await _load_chunks(maker, note_id)
    anchor = str(chunks[0].id)
    result = parse_epic(_FIXTURE.read_text())
    catches = await integrate_parse_result(
        _pipeline(maker),
        maker,
        SYSTEM_CTX,
        note_id=uuid.UUID(note_id),
        note_domain="health",
        captured_at=datetime.now(UTC),
        chunks=chunks,
        result=result,
        chunk_for_anchor=lambda _a: anchor,
    )
    assert catches == []  # clean Epic emits no location-lock facts
    return note_id


async def test_epic_mints_health_domain_entities(maker, tmp_path):  # noqa: F811
    await _integrate(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        obs = (
            (await s.execute(select(Entity).where(Entity.canonical_name == "Platelet count")))
            .scalars()
            .all()
        )
        assert obs and all(e.domain_code == "health" for e in obs)
        assert all(e.kind == "Observation" for e in obs)

        enc = (
            (await s.execute(select(Entity).where(func.lower(Entity.kind) == "encounter")))
            .scalars()
            .all()
        )
        assert enc and all(e.domain_code == "health" for e in enc)

        person = (
            (await s.execute(select(Entity).where(Entity.canonical_name == "Chen, Sarah MD")))
            .scalars()
            .all()
        )
        assert person and all(e.domain_code == "health" for e in person)

        cond = (
            (await s.execute(select(Entity).where(func.lower(Entity.kind) == "medicalcondition")))
            .scalars()
            .all()
        )
        assert cond


async def test_final_readings_commit_active_value_facts(maker, tmp_path):  # noqa: F811
    note_id = await _integrate(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        active_values = (
            (
                await s.execute(
                    select(Fact).where(
                        Fact.note_id == uuid.UUID(note_id),
                        Fact.predicate == "value",
                        Fact.status == "active",
                    )
                )
            )
            .scalars()
            .all()
        )
        # The final lab readings (hemoglobin, potassium, WBC, ...) commit active.
        assert active_values
        assert all(f.domain_code == "health" for f in active_values)


async def test_corrected_without_original_is_held_for_review(maker, tmp_path):  # noqa: F811
    # The A3 platelet is `corrected` but has no prior reading at its draw. Only the
    # status-aware transition (fhir_status reaching decide) produces this: the value
    # fact is held pending_review with a correction_without_original card.
    note_id = await _integrate(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        pending = (
            (
                await s.execute(
                    select(Fact).where(
                        Fact.note_id == uuid.UUID(note_id),
                        Fact.predicate == "value",
                        Fact.status == "pending_review",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert pending, "the corrected-without-original platelet must be held"

        cards = (
            (await s.execute(select(ReviewItem).where(ReviewItem.kind == "low_confidence")))
            .scalars()
            .all()
        )
        assert any((c.payload or {}).get("subkind") == "correction_without_original" for c in cards)
