"""Unit tests for the eval gate logic (tests/eval/assertions.check_case).

Pure, CI-runnable: proves the HARNESS itself catches the production-bug classes
(sentence-as-value, minted-name entity, missing/forbidden fact) and passes a
clean intent — so a green real-Grok run actually means something.
"""

from typing import Any, cast

from jbrain.analysis.arbiter import ArbiterPlan, PlannedFact
from jbrain.analysis.intent import EntityResolution, IntegrationIntent, IntentFact
from jbrain.analysis.weight import CommitStatus
from tests.eval.assertions import check_case
from tests.eval.cases import case_from_dict


def _fact(entity: str, predicate: str, **kw: Any) -> IntentFact:
    base: dict[str, Any] = dict(
        entity_ref=entity,
        predicate=predicate,
        qualifier="",
        kind="attribute",
        statement="",
        value_json=None,
        assertion="asserted",
        object_entity_ref=None,
        temporal=None,
        attested_span=None,
        self_confidence=0.9,
        inferred=False,
    )
    base.update(kw)
    return IntentFact(**base)


def _intent(resolutions: list[EntityResolution], facts: list[IntentFact]) -> IntegrationIntent:
    return IntegrationIntent(
        note_id="n1",
        schema_version=1,
        prompt_version="v",
        integrator_version="i",
        entity_resolutions=resolutions,
        facts=facts,
    )


def _plan(facts: list[IntentFact], statuses: list[str]) -> ArbiterPlan:
    return ArbiterPlan(
        rejected=False,
        fatal_violations=(),
        facts=tuple(
            PlannedFact(fact=f, weight=0.9, status=cast(CommitStatus, s))
            for f, s in zip(facts, statuses, strict=True)
        ),
        merge_proposals=(),
        distinct_proposals=(),
    )


def _clean_case() -> Any:
    return case_from_dict(
        {
            "id": "c",
            "note_text": "n",
            "expect": {
                "resolutions": [
                    {"mention": "Me", "mode": "existing", "entity_id": "owner-1"},
                    {"mention": "Celine", "mode": "new"},
                ],
                "forbidden_entities": ["Sammy"],
                "facts": [
                    {
                        "entity": "Celine",
                        "predicate": "name.full",
                        "kind": "attribute",
                        "value": "Celine Kitina Hopkins",
                        "disposition": "commit",
                    }
                ],
            },
        }
    )


def _clean_intent_plan():
    facts = [_fact("Celine", "name.full", value_json={"value": "Celine Kitina Hopkins"})]
    intent = _intent(
        [
            EntityResolution(mention_ref="Me", mode="existing", proposed_entity_id="owner-1"),
            EntityResolution(mention_ref="Celine", mode="new", new_name="Celine"),
        ],
        facts,
    )
    return intent, _plan(facts, ["active"])


def test_clean_intent_passes():
    intent, plan = _clean_intent_plan()
    assert check_case(_clean_case(), intent, plan) == []


def test_value_json_none_is_caught_as_sentence_regression():
    # The exact production bug: value_json dropped -> the value lives in the
    # statement sentence. The gate must FAIL this.
    facts = [_fact("Celine", "name.full", value_json=None, statement="Celine's name is ...")]
    intent = _intent([EntityResolution("Celine", "new", new_name="Celine")], facts)
    fails = check_case(_clean_case(), intent, _plan(facts, ["active"]))
    assert any("value_json is None" in f for f in fails)


def test_wrong_value_is_caught():
    facts = [_fact("Celine", "name.full", value_json={"value": "Celine Hopkins"})]  # short, wrong
    intent = _intent([EntityResolution("Celine", "new", new_name="Celine")], facts)
    fails = check_case(_clean_case(), intent, _plan(facts, ["active"]))
    assert any("value" in f and "!=" in f for f in fails)


def test_minted_forbidden_name_is_caught():
    # "Sammy" minted as its own entity — the other production bug.
    facts = [_fact("Celine", "name.full", value_json={"value": "Celine Kitina Hopkins"})]
    intent = _intent(
        [
            EntityResolution("Celine", "new", new_name="Celine"),
            EntityResolution("Sammy", "new", new_name="Sammy"),
        ],
        facts,
    )
    fails = check_case(_clean_case(), intent, _plan(facts, ["active"]))
    assert any("forbidden entity minted: 'Sammy'" in f for f in fails)


def test_missing_required_fact_is_caught():
    intent = _intent([EntityResolution("Celine", "new", new_name="Celine")], [])
    assert any("not found" in f for f in check_case(_clean_case(), intent, _plan([], [])))


def test_resolution_mode_mismatch_is_caught():
    # Celine expected new, but resolved existing.
    facts = [_fact("Celine", "name.full", value_json={"value": "Celine Kitina Hopkins"})]
    intent = _intent(
        [
            EntityResolution("Me", "existing", proposed_entity_id="owner-1"),
            EntityResolution("Celine", "existing", proposed_entity_id="ent-x"),
        ],
        facts,
    )
    fails = check_case(_clean_case(), intent, _plan(facts, ["active"]))
    assert any("mode" in f for f in fails)


def test_disposition_mismatch_is_caught():
    # Expected commit (active) but the plan held it for review.
    facts = [_fact("Celine", "name.full", value_json={"value": "Celine Kitina Hopkins"})]
    intent = _intent([EntityResolution("Celine", "new", new_name="Celine")], facts)
    fails = check_case(_clean_case(), intent, _plan(facts, ["pending_review"]))
    assert any("disposition" in f for f in fails)


def test_absent_fact_present_is_caught():
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "n",
            "expect": {"absent_facts": [{"entity": "Me", "predicate": "diabetes"}]},
        }
    )
    facts = [_fact("Me", "diabetes", kind="state")]
    intent = _intent([EntityResolution("Me", "existing", proposed_entity_id="owner-1")], facts)
    assert any(
        "forbidden fact present" in f for f in check_case(case, intent, _plan(facts, ["active"]))
    )


def test_max_facts_bound_is_caught():
    case = case_from_dict({"id": "c", "note_text": "n", "expect": {"max_facts": 1}})
    facts = [_fact("Me", "a"), _fact("Me", "b")]
    intent = _intent([], facts)
    assert any(
        "too many facts" in f for f in check_case(case, intent, _plan(facts, ["active", "active"]))
    )


def test_max_facts_advisory_bound_reports_with_advisory_prefix():
    # The tightened uncalibrated bound (max_facts_advisory) reports as an
    # "advisory:"-prefixed miss the runner never hard-fails on — while the
    # calibrated hard bound stays silent when respected.
    case = case_from_dict(
        {"id": "c", "note_text": "n", "expect": {"max_facts": 5, "max_facts_advisory": 1}}
    )
    facts = [_fact("Me", "a"), _fact("Me", "b")]
    intent = _intent([], facts)
    fails = check_case(case, intent, _plan(facts, ["active", "active"]))
    assert fails and all(f.startswith("advisory:") for f in fails)
    # Within the tightened bound → no report at all.
    one = [_fact("Me", "a")]
    assert check_case(case, _intent([], one), _plan(one, ["active"])) == []


def test_max_entities_catches_duplicate_or_junk():
    # The no-duplicate gate: owner doesn't count; two non-owner entities exceed 1.
    case = case_from_dict({"id": "c", "note_text": "n", "expect": {"max_entities": 1}})
    intent = _intent(
        [
            EntityResolution("Me", "existing", proposed_entity_id="owner-1"),
            EntityResolution("Celine", "new", new_name="Celine Kitina Hopkins"),
            EntityResolution("Sammy", "new", new_name="Sammy"),  # the junk duplicate
        ],
        [],
    )
    assert any("too many entities" in f for f in check_case(case, intent, _plan([], [])))


def test_max_entities_passes_when_owner_plus_one():
    case = case_from_dict({"id": "c", "note_text": "n", "expect": {"max_entities": 1}})
    intent = _intent(
        [
            EntityResolution("Me", "existing", proposed_entity_id="owner-1"),
            EntityResolution("Celine", "new", new_name="Celine"),
        ],
        [],
    )
    assert check_case(case, intent, _plan([], [])) == []


def test_supersede_proposal_required_and_missing():
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "n",
            "expect": {"supersede": [{"entity": "Me", "predicate": "worksFor"}]},
        }
    )
    intent = _intent([], [])
    assert any("supersede" in f for f in check_case(case, intent, _plan([], [])))
