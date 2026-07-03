"""The EmrImporter lowering (docs/plans/EMR_IMPORT_PLAN.md §6.6): parser
candidates -> IntegrationIntents. Structural — every emitted intent must pass the
shipped `validate_intent` (no fatal violations), carry fhir_status on lab values,
group a facility transfer into ONE intent so partOfEncounter resolves intra-intent,
and produce zero firewall catches on clean Epic input.
"""

from __future__ import annotations

from pathlib import Path

from jbrain.analysis.intent import has_fatal, validate_intent
from jbrain.ingest.emr.epic import parse_epic
from jbrain.ingest.emr.importer import lower_parse_result

_TEXT = (Path(__file__).resolve().parents[1] / "fixtures" / "emr" / "epic_report.txt").read_text()
_RESULT = parse_epic(_TEXT)


def _chunk_for(anchor: str) -> str:
    return f"chunk-{anchor.replace(' ', '-')}"


_INTENTS, _CATCHES = lower_parse_result(_RESULT, "note-1", _chunk_for)


def _facts(pred=lambda f: True):
    return [f for intent in _INTENTS for f in intent.facts if pred(f)]


def test_one_intent_per_episode_transfer_grouped() -> None:
    # MICU + A3 (a transfer) -> ONE episode intent; the two outpatient visits ->
    # their own intents. 3 intents total.
    assert len(_INTENTS) == 3
    # Exactly one intent carries both inpatient encounters (the transfer episode).
    episode = [
        i
        for i in _INTENTS
        if sum(
            1 for f in i.facts if f.predicate == "period" and f.value_json == {"value": "inpatient"}
        )
        == 2
    ]
    assert len(episode) == 1


def test_every_intent_is_structurally_valid() -> None:
    for intent in _INTENTS:
        assert not has_fatal(validate_intent(intent)), validate_intent(intent)


def test_value_fact_carries_fhir_status() -> None:
    plt_corrected = [
        f
        for f in _facts(lambda f: f.predicate == "value")
        if f.fhir_status == "corrected" and f.value_json == {"value": 9.0, "unit": "10*3/uL"}
    ]
    assert len(plt_corrected) == 1
    # Non-lab predicates never carry a status.
    assert all(f.fhir_status is None for f in _facts(lambda f: f.predicate != "value"))


def test_per_draw_qualifier_and_analyte_constant_qualifier() -> None:
    values = _facts(lambda f: f.predicate == "value")
    assert all("|" in f.qualifier for f in values)  # <collected_iso>|<specimen>
    # identifier is keyed by the constant scheme, category by the empty qualifier —
    # one functional fact per analyte, not one per draw.
    idents = _facts(lambda f: f.predicate == "identifier")
    assert idents and all(f.qualifier == "loinc" for f in idents)
    cats = _facts(lambda f: f.predicate == "category")
    assert cats and all(f.qualifier == "" for f in cats)


def test_part_of_encounter_edge_is_intra_intent() -> None:
    part_of = _facts(lambda f: f.predicate == "partOfEncounter")
    assert len(part_of) == 1
    # its object ref resolves within the same intent
    owner = next(i for i in _INTENTS if part_of[0] in i.facts)
    refs = {r.mention_ref for r in owner.entity_resolutions}
    assert part_of[0].object_entity_ref in refs


def test_has_observation_join_one_per_draw() -> None:
    has_obs = _facts(lambda f: f.predicate == "hasObservation")
    values = _facts(lambda f: f.predicate == "value")
    assert len(has_obs) == len(values)


def test_effective_date_carries_a_point_temporal() -> None:
    eff = _facts(lambda f: f.predicate == "effectiveDate")
    assert eff and all(
        f.temporal is not None
        and f.temporal.precision == "instant"
        and f.temporal.resolved_start is not None
        for f in eff
    )


def test_no_firewall_catches_on_clean_epic() -> None:
    assert _CATCHES == []
