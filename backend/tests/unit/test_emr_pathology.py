"""The pathology-narrative diagnosis extraction (docs/plans/EMR_IMPORT_PLAN.md
§6.5) — the one LLM touch on the structured path, faked here (non-neg #1/#5).

Covers the extractor's parse + gates and the importer's lowering: an affirmed
high-confidence Final Diagnosis becomes ONE `encounterDiagnosis` edge on the
hospitalization; a rule-out stays hypothetical prose (never a diagnosis fact);
a below-floor diagnosis is left in the prose; and the whole thing fails soft when
the LLM is unrouted or unusable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jbrain.analysis.intent import has_fatal, validate_intent
from jbrain.ingest.emr.epic import parse_epic
from jbrain.ingest.emr.importer import lower_parse_result
from jbrain.ingest.emr.pathology import (
    CONFIDENCE_FLOOR,
    PATHOLOGY_TASK,
    PathologyDiagnosis,
    extract_pathology_diagnoses,
)
from jbrain.llm import FakeLlmClient, LlmError, LlmRouter

_TEXT = (Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "epic_report.txt").read_text()
_RESULT = parse_epic(_TEXT)


def _router(payload: object) -> LlmRouter:
    """A router that routes the pathology task (by task AND by the prompt's `low`
    tier) to a fake returning one scripted JSON payload."""
    fake = FakeLlmClient(responses=(json.dumps(payload),))
    return LlmRouter(
        {"xai": fake},
        {PATHOLOGY_TASK: ("xai", "grok")},
        tiers={"low": ("xai", "grok")},
    )


def _chunk_for(anchor: str) -> str:
    return f"chunk-{anchor.replace(' ', '-')}"


# --- extractor: parse + shape guards --------------------------------------


async def test_extractor_parses_and_clamps() -> None:
    router = _router(
        {
            "diagnoses": [
                {
                    "condition": " thrombocytopenia ",
                    "icd10": "D69.6",
                    "ruled_out": False,
                    "confidence": 1.4,
                },
                {
                    "condition": "marrow process",
                    "icd10": None,
                    "ruled_out": True,
                    "confidence": 0.2,
                },
            ]
        }
    )
    got = await extract_pathology_diagnoses(router, "Final Diagnosis: thrombocytopenia")
    assert len(got) == 2
    assert got[0] == PathologyDiagnosis("thrombocytopenia", "D69.6", False, 1.0)  # clamped, trimmed
    assert got[1].ruled_out and got[1].icd10 is None


async def test_extractor_drops_malformed_rows() -> None:
    router = _router(
        {
            "diagnoses": [
                {"icd10": "D69.6", "ruled_out": False, "confidence": 0.9},  # no condition
                {"condition": "anemia", "ruled_out": False, "confidence": "high"},  # bad confidence
                {
                    "condition": "hypocellular marrow",
                    "icd10": None,
                    "ruled_out": False,
                    "confidence": 0.9,
                },
            ]
        }
    )
    got = await extract_pathology_diagnoses(router, "narrative")
    assert [d.condition for d in got] == ["hypocellular marrow"]


async def test_extractor_empty_narrative_skips_llm() -> None:
    fake = FakeLlmClient(responses=('{"diagnoses": []}',))
    router = LlmRouter(
        {"xai": fake}, {PATHOLOGY_TASK: ("xai", "grok")}, tiers={"low": ("xai", "grok")}
    )
    assert await extract_pathology_diagnoses(router, "   ") == []
    assert fake.calls == []  # no LLM call for empty prose


async def test_extractor_unrouted_task_fails_soft() -> None:
    # A router that carries only note.extract (the harness shape) can't route the
    # pathology task -> [] with no call, never an error.
    fake = FakeLlmClient()
    router = LlmRouter({"xai": fake}, {"note.extract": ("xai", "grok")})
    assert await extract_pathology_diagnoses(router, "Final Diagnosis: x") == []
    assert fake.calls == []


async def test_extractor_unusable_response_fails_soft() -> None:
    router = _router("not json at all")
    assert await extract_pathology_diagnoses(router, "Final Diagnosis: x") == []


async def test_extractor_provider_error_fails_soft() -> None:
    # A provider that raises mid-call must not fail the deterministic import: the
    # extractor swallows it and returns [] (the narrative is still searchable).
    class _Boom(FakeLlmClient):
        async def complete(self, **kwargs):
            raise LlmError("provider exploded")

    router = LlmRouter(
        {"xai": _Boom()},
        {PATHOLOGY_TASK: ("xai", "grok")},
        tiers={"low": ("xai", "grok")},
    )
    assert await extract_pathology_diagnoses(router, "Final Diagnosis: x") == []


@pytest.mark.parametrize(
    "payload", [{"diagnoses": "not-a-list"}, {"diagnoses": ["not-a-dict"]}, []]
)
async def test_extractor_non_shaped_payload_yields_nothing(payload: object) -> None:
    router = _router(payload)
    assert await extract_pathology_diagnoses(router, "Final Diagnosis: x") == []


# --- committable gate -----------------------------------------------------


@pytest.mark.parametrize(
    ("dx", "committable"),
    [
        (PathologyDiagnosis("thrombocytopenia", "D69.6", False, 0.95), True),
        (PathologyDiagnosis("marrow process", None, True, 0.95), False),  # rule-out
        (PathologyDiagnosis("anemia", None, False, CONFIDENCE_FLOOR - 0.01), False),  # below floor
        (PathologyDiagnosis("anemia", None, False, CONFIDENCE_FLOOR), True),  # at floor
    ],
)
def test_committable_gate(dx: PathologyDiagnosis, committable: bool) -> None:
    assert dx.committable is committable


# --- lowering into the intent ---------------------------------------------


def _diagnosis_facts(intents):
    return [f for i in intents for f in i.facts if f.predicate == "encounterDiagnosis"]


def test_lowering_emits_edge_for_affirmed_diagnosis_only() -> None:
    diagnoses = [
        PathologyDiagnosis("hypocellular marrow", None, False, 0.92),
        PathologyDiagnosis("primary marrow process", None, True, 0.3),  # rule-out -> dropped
        PathologyDiagnosis("low-confidence finding", None, False, 0.4),  # below floor -> dropped
    ]
    intents, catches = lower_parse_result(
        _RESULT, "note-1", _chunk_for, pathology_diagnoses=diagnoses
    )
    assert catches == []
    for intent in intents:
        assert not has_fatal(validate_intent(intent))

    path_edges = [
        f for f in _diagnosis_facts(intents) if f.statement.startswith("pathology diagnosis:")
    ]
    assert len(path_edges) == 1
    edge = path_edges[0]
    assert edge.statement == "pathology diagnosis: hypocellular marrow"
    assert edge.assertion == "asserted"
    assert edge.self_confidence == pytest.approx(0.92)
    # Attributed to the inpatient hospitalization (the MICU episode head), not a lab visit.
    assert edge.entity_ref.startswith("enc:")
    # The minted MedicalCondition is the edge's object.
    obj = next(
        r for i in intents for r in i.entity_resolutions if r.mention_ref == edge.object_entity_ref
    )
    assert obj.new_kind == "MedicalCondition"
    assert obj.new_name == "hypocellular marrow"


def test_lowering_noop_without_diagnoses() -> None:
    # No pathology diagnoses -> byte-identical to the pre-pathology lowering.
    base, _ = lower_parse_result(_RESULT, "note-1", _chunk_for)
    assert not _diagnosis_facts(base) or all(
        not f.statement.startswith("pathology diagnosis:") for f in _diagnosis_facts(base)
    )
