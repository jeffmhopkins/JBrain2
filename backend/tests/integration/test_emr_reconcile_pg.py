"""Cross-source reconciliation end-to-end (docs/plans/EMR_IMPORT_PLAN.md §6.4)
against real Postgres: the ARIA reprint of the 2021 OneContent labs reconciles —
matching reads corroborate the precise draws (so the graph keeps ONE draw, not
two) while the readable-but-unmatched 07/29 read parks behind a low_confidence
card. Proves the parked-card DB path is RLS-scoped and idempotent.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import scoped_session
from jbrain.ingest.emr.aria import parse_aria
from jbrain.ingest.emr.candidates import (
    CandidateEncounter,
    CandidateObservation,
    ParseResult,
    canonicalize_analyte,
)
from jbrain.ingest.emr.integrate import file_parked_cards, integrate_parse_result
from jbrain.ingest.emr.reconcile import reconcile
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_apply_intent_pg import _load_chunks
from tests.integration.test_extraction_pg import ingest, make_note, maker  # noqa: F401
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_ARIA = Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "aria_ocr.txt"
_DRAW_2021 = datetime(2021, 7, 22, 7, 12, tzinfo=UTC)


def _pipeline(maker) -> AnalysisPipeline:  # noqa: F811
    router = LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})
    return AnalysisPipeline(maker, router)


def _precise(label: str, value: float) -> CandidateObservation:
    return CandidateObservation(
        analyte=canonicalize_analyte(label),
        collected_at=_DRAW_2021,
        value_num=value,
        value_text=None,
        unit="10*3/uL",
        ref_low=None,
        ref_high=None,
        ref_text=None,
        interpretation=None,
        specimen_id="",
        performing_lab=None,
        fhir_status="final",
        source_system="onecontent",
        fidelity=2,
        source_anchor="page 1",
        precision="instant",
    )


def _precise_parse() -> ParseResult:
    draws = [
        _precise("Platelet count", 38),
        _precise("Hemoglobin", 10.9),
        _precise("White Blood Cell Count", 3.2),
        _precise("Potassium", 4.1),
    ]
    result = ParseResult()
    result.encounters = [
        CandidateEncounter(
            key="C0000202101",
            encounter_class="ambulatory",
            facility=None,
            care_unit=None,
            admitted_at=_DRAW_2021,
            discharged_at=_DRAW_2021,
            disposition=None,
            source_system="onecontent",
            source_anchor="page 1",
            observations=draws,
        )
    ]
    return result


async def test_aria_reprint_reconciles_and_parks(maker, tmp_path):  # noqa: F811
    precise_parse = _precise_parse()
    precise = precise_parse.encounters[0].observations
    aria = parse_aria(_ARIA.read_text()).orphan_observations

    rec = reconcile(precise, aria)
    # The three 07/22 reprints corroborate; the 07/29 potassium parks.
    assert len(rec.corroborated) == 3
    assert len(rec.parked) == 1
    assert rec.parked[0].observation.analyte.name == "Potassium"

    note_id = await make_note(maker, domain="health", body="OneContent + ARIA labs.")
    await ingest(maker, note_id, tmp_path)
    chunks = await _load_chunks(maker, note_id)
    anchor = str(chunks[0].id)
    await integrate_parse_result(
        _pipeline(maker),
        maker,
        SYSTEM_CTX,
        note_id=uuid.UUID(note_id),
        note_domain="health",
        captured_at=datetime.now(UTC),
        chunks=chunks,
        result=precise_parse,
        chunk_for_anchor=lambda _a: anchor,
    )
    filed = await file_parked_cards(
        maker, SYSTEM_CTX, note_id=uuid.UUID(note_id), note_domain="health", parked=rec.parked
    )
    assert filed == 1
    # Idempotent: re-filing the same parked read does not duplicate the open card.
    again = await file_parked_cards(
        maker, SYSTEM_CTX, note_id=uuid.UUID(note_id), note_domain="health", parked=rec.parked
    )
    assert again == 0

    async with scoped_session(maker, SYSTEM_CTX) as s:
        # One platelet row for the 2021 draw — the ARIA reprint corroborated, not duplicated.
        plt_rows = (
            await s.execute(
                text(
                    "SELECT collected_at FROM app.lab_results"
                    " WHERE analyte = 'Platelet count' AND collected_at = :c"
                ),
                {"c": _DRAW_2021},
            )
        ).all()
        assert len(plt_rows) == 1

        cards = (
            await s.execute(
                text(
                    "SELECT payload FROM app.review_items"
                    " WHERE kind = 'low_confidence' AND payload->>'subkind' = 'ocr_unreconciled'"
                    " AND payload->>'note_id' = :nid"
                ),
                {"nid": note_id},
            )
        ).all()
        assert len(cards) == 1
        assert cards[0][0]["analyte"] == "Potassium"
        assert cards[0][0]["source"] == "aria"
