"""End-to-end deterministic EMR integration against real Postgres: the Epic
fixture parses, lowers, and commits through the SHIPPED arbiter
(plan_intent -> apply_intent) into health-domain graph facts
(docs/plans/EMR_IMPORT_PLAN.md §6.6). The LLM is never called. Proves the
importer's IntegrationIntents mint the expected entities and that `fhir_status`
threads all the way into `decide` — the fixture's `corrected` platelet has no
prior reading at its draw, so it is held for review (the §3.5 red-team outcome),
which only the status-aware transition produces.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.emr.epic import parse_epic
from jbrain.ingest.emr.integrate import integrate_parse_result
from jbrain.ingest.emr.pathology import PATHOLOGY_TASK
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


async def test_lab_results_projection_populated(maker, tmp_path):  # noqa: F811
    # project_emr runs inside _apply, so the projection is materialized already.
    await _integrate(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT analyte, value_num, unit, loinc, report_status, is_current,"
                        " collected_at FROM app.lab_results"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert rows
        plt = [r for r in rows if r["analyte"] == "Platelet count"]
        assert plt and any(r["loinc"] == "777-3" for r in plt)
        assert all(r["collected_at"] is not None for r in rows)
        # The corrected-without-original platelet is not current -> preliminary.
        not_current = [r for r in plt if not r["is_current"]]
        assert not_current and all(r["report_status"] == "preliminary" for r in not_current)
        # Final readings are current.
        finals = [r for r in rows if r["is_current"]]
        assert finals and all(r["report_status"] == "final" for r in finals)


async def test_encounters_projection_populated(maker, tmp_path):  # noqa: F811
    await _integrate(maker, tmp_path)
    async with scoped_session(maker, SYSTEM_CTX) as s:
        encs = (
            (
                await s.execute(
                    text(
                        "SELECT class AS enc_class, facility, care_unit, admitted_at,"
                        " discharged_at, los_days, part_of_id FROM app.encounters"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert encs
        micu = [e for e in encs if e["care_unit"] == "MICU"]
        assert micu, "the MICU inpatient encounter projected"
        assert micu[0]["enc_class"] == "inpatient"
        assert micu[0]["los_days"] == 3  # 01/25 -> 01/28

        a3 = [e for e in encs if e["care_unit"] == "A3"]
        assert a3 and a3[0]["part_of_id"] is not None  # the transfer linkage

        providers = (
            (await s.execute(text("SELECT provider_name, role FROM app.encounter_providers")))
            .mappings()
            .all()
        )
        assert any(
            p["provider_name"] == "Chen, Sarah MD" and p["role"] == "attending" for p in providers
        )

        diagnoses = (
            (await s.execute(text("SELECT icd10 FROM app.encounter_diagnoses"))).mappings().all()
        )
        assert any(d["icd10"] == "D69.6" for d in diagnoses)


async def test_read_labs_tool_returns_records_and_firewalls(maker, tmp_path):  # noqa: F811
    from jbrain.agent.labtools import build_lab_handlers
    from jbrain.agent.loop import ToolContext, ToolOutput
    from jbrain.db.session import SessionContext

    await _integrate(maker, tmp_path)
    handlers = build_lab_handlers(maker)
    owner = ToolContext(session=SYSTEM_CTX, scopes=())
    out = await handlers["read_labs"]({"analyte": "platelet"}, owner)
    assert "Platelet count" in out
    # A single-analyte trend also emits a lab_chart view; when present it is a well-
    # formed, health-domain plot (the exact shape is pinned in test_lab_chart_view.py).
    trend = await handlers["read_labs"]({"analyte": "platelet", "trend": True}, owner)
    if isinstance(trend, ToolOutput) and trend.view is not None:
        assert trend.view.view == "lab_chart"
        assert trend.view.data["domain"] == "health"
        assert len(trend.view.data["series"][0]["points"]) >= 2
    # A general-only scope sees nothing — the firewall is the tooth (§5, §7.2): no rows,
    # and therefore no view can leak a health reading through the render channel.
    general = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    empty = await handlers["read_labs"](
        {"analyte": "platelet", "trend": True}, ToolContext(session=general, scopes=())
    )
    assert "No lab results" in empty
    assert isinstance(empty, ToolOutput) and empty.view is None


def _pathology_pipeline(maker, payload: object) -> AnalysisPipeline:  # noqa: F811
    """A pipeline whose router routes the one pathology LLM touch (§6.5) to a fake
    returning a scripted Final-Diagnosis payload."""
    fake = FakeLlmClient(responses=(json.dumps(payload),))
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), PATHOLOGY_TASK: ("xai", "grok")},
        tiers={"low": ("xai", "grok")},
    )
    return AnalysisPipeline(maker, router)


async def test_pathology_final_diagnosis_commits_affirmed_only(maker, tmp_path):  # noqa: F811
    # The Final Diagnosis line yields a small, high-confidence set (§6.5): an
    # affirmed diagnosis becomes an encounterDiagnosis edge on the hospitalization;
    # a rule-out stays hypothetical prose and is never a diagnosis fact.
    payload = {
        "diagnoses": [
            {
                "condition": "hypocellular marrow",
                "icd10": None,
                "ruled_out": False,
                "confidence": 0.93,
            },
            {
                "condition": "evolving primary marrow process",
                "icd10": None,
                "ruled_out": True,
                "confidence": 0.3,
            },
        ]
    }
    note_id = await make_note(maker, domain="health", body="Imported EMR records.")
    await ingest(maker, note_id, tmp_path)
    chunks = await _load_chunks(maker, note_id)
    anchor = str(chunks[0].id)
    result = parse_epic(_FIXTURE.read_text())
    await integrate_parse_result(
        _pathology_pipeline(maker, payload),
        maker,
        SYSTEM_CTX,
        note_id=uuid.UUID(note_id),
        note_domain="health",
        captured_at=datetime.now(UTC),
        chunks=chunks,
        result=result,
        chunk_for_anchor=lambda _a: anchor,
    )
    async with scoped_session(maker, SYSTEM_CTX) as s:
        conds = {
            e.canonical_name
            for e in (
                await s.execute(select(Entity).where(func.lower(Entity.kind) == "medicalcondition"))
            )
            .scalars()
            .all()
        }
        assert "hypocellular marrow" in conds  # the affirmed diagnosis is minted
        assert "evolving primary marrow process" not in conds  # the rule-out is not
        # The pathology diagnosis projects onto the inpatient encounter's diagnosis list.
        labels = [
            r["label"]
            for r in (await s.execute(text("SELECT label FROM app.encounter_diagnoses")))
            .mappings()
            .all()
        ]
        assert "hypocellular marrow" in labels
        assert "evolving primary marrow process" not in labels


async def test_read_encounters_tool_lists_and_expands(maker, tmp_path):  # noqa: F811
    import re

    from jbrain.agent.labtools import build_lab_handlers
    from jbrain.agent.loop import ToolContext

    await _integrate(maker, tmp_path)
    handlers = build_lab_handlers(maker)
    owner = ToolContext(session=SYSTEM_CTX, scopes=())
    listing = await handlers["read_encounters"]({}, owner)
    assert "inpatient" in listing and "MICU" in listing
    m = re.search(r"\[([0-9a-f-]{36})\] inpatient[^\n]*MICU", listing)
    assert m, listing
    detail = await handlers["read_encounters"]({"encounter_id": m.group(1)}, owner)
    assert "Chen, Sarah MD" in detail and "D69.6" in detail
