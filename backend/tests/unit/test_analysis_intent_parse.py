"""Unit tests for parse_intent (Wave 1 Track B, B1).

Pure: the integrate.note JSON → typed IntegrationIntent, strict on shape,
lenient on items, with predicate normalization (I4) applied at parse time.
"""

from typing import Any

import pytest

from jbrain.analysis.intent_parse import IntentParseError, parse_intent
from jbrain.schema import get_registry

_PROV: dict[str, Any] = dict(
    note_id="n1", schema_version=1, prompt_version="v1", integrator_version="i1"
)


def _payload(**kw):
    base = {"resolutions": [], "facts": []}
    base.update(kw)
    return base


def _fact(**kw):
    base = {
        "entity_ref": "m1",
        "predicate": "spouse",
        "kind": "relationship",
        "assertion": "asserted",
        "statement": "married to Celine",
        "self_confidence": 0.9,
    }
    base.update(kw)
    return base


def test_non_dict_payload_raises():
    with pytest.raises(IntentParseError):
        parse_intent(["not", "a", "dict"], **_PROV)


def test_missing_top_level_lists_raise():
    with pytest.raises(IntentParseError):
        parse_intent({"resolutions": []}, **_PROV)  # facts missing
    with pytest.raises(IntentParseError):
        parse_intent({"facts": []}, **_PROV)  # resolutions missing


def test_parses_resolution_and_fact():
    payload = _payload(
        resolutions=[{"mention_ref": "m1", "mode": "existing", "entity_id": "e1"}],
        facts=[_fact()],
    )
    intent = parse_intent(payload, **_PROV)
    assert intent.note_id == "n1"
    assert len(intent.entity_resolutions) == 1
    assert intent.entity_resolutions[0].proposed_entity_id == "e1"
    assert len(intent.facts) == 1
    assert intent.facts[0].entity_ref == "m1"


def test_predicate_is_normalized_at_parse_time():
    # I4: the agent's drift spelling is canonicalized before keying. Compute the
    # expected via the registry so the test doesn't hardcode the mapping.
    expected = get_registry().normalize_predicate("legalName")
    payload = _payload(facts=[_fact(predicate="legalName", kind="attribute")])
    intent = parse_intent(payload, **_PROV)
    assert intent.facts[0].predicate == expected


def test_bad_kind_or_assertion_fact_is_dropped():
    payload = _payload(
        facts=[_fact(kind="nonsense"), _fact(assertion="maybe"), _fact()],
    )
    intent = parse_intent(payload, **_PROV)
    assert len(intent.facts) == 1  # only the valid one survives


def test_resolution_with_bad_mode_is_dropped():
    payload = _payload(resolutions=[{"mention_ref": "m1", "mode": "guess"}])
    assert parse_intent(payload, **_PROV).entity_resolutions == []


def test_self_confidence_clamped_and_defaulted():
    payload = _payload(
        facts=[_fact(self_confidence=1.5), _fact(entity_ref="m2", self_confidence=None)],
    )
    facts = parse_intent(payload, **_PROV).facts
    assert facts[0].self_confidence == 1.0
    assert facts[1].self_confidence == 0.5  # missing/garbage → middling


def test_attested_span_built_from_chunk_and_surface():
    payload = _payload(facts=[_fact(chunk_id="c1", surface="Celine")])
    span = parse_intent(payload, **_PROV).facts[0].attested_span
    assert span is not None and span.chunk_id == "c1" and span.surface == "Celine"


def test_temporal_parsed():
    payload = _payload(
        facts=[
            _fact(
                temporal={
                    "phrase": "last June",
                    "resolved_start": "2021-06-01T00:00:00Z",
                    "precision": "month",
                }
            )
        ],
    )
    t = parse_intent(payload, **_PROV).facts[0].temporal
    assert t is not None and t.phrase == "last June" and t.precision == "month"
    assert t.resolved_start is not None and t.resolved_start.year == 2021


def test_merge_distinct_and_supersession_parsed():
    payload = _payload(
        merge_proposals=[{"entity_a_id": "e1", "entity_b_id": "e2"}],
        distinct_proposals=[{"entity_a_id": "e3", "entity_b_id": "e4"}],
        supersession_proposals=[
            {"entity_ref": "m1", "predicate": "employer", "action": "supersede"}
        ],
    )
    intent = parse_intent(payload, **_PROV)
    assert len(intent.merge_proposals) == 1
    assert len(intent.distinct_proposals) == 1
    assert len(intent.supersession_proposals) == 1
    assert intent.supersession_proposals[0].action == "supersede"


def test_bad_supersession_action_dropped():
    payload = _payload(
        supersession_proposals=[{"entity_ref": "m1", "predicate": "x", "action": "delete"}]
    )
    assert parse_intent(payload, **_PROV).supersession_proposals == []


def test_pair_missing_id_dropped():
    payload = _payload(merge_proposals=[{"entity_a_id": "e1"}])
    assert parse_intent(payload, **_PROV).merge_proposals == []


def test_oversized_statement_is_truncated():
    from jbrain.analysis.extraction import MAX_STATEMENT_CHARS

    payload = _payload(facts=[_fact(statement="x" * (MAX_STATEMENT_CHARS + 500))])
    assert len(parse_intent(payload, **_PROV).facts[0].statement) == MAX_STATEMENT_CHARS


def test_oversized_value_json_is_dropped():
    payload = _payload(facts=[_fact(value_json={"k": "v" * 20000})])
    assert parse_intent(payload, **_PROV).facts[0].value_json is None


def test_non_dict_value_json_becomes_none():
    payload = _payload(facts=[_fact(value_json=["not", "a", "dict"])])
    assert parse_intent(payload, **_PROV).facts[0].value_json is None


def test_object_entity_ref_passthrough():
    payload = _payload(facts=[_fact(object_entity_ref="m2")])
    assert parse_intent(payload, **_PROV).facts[0].object_entity_ref == "m2"
