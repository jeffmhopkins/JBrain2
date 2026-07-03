"""The status-aware measurement-supersession exception for EMR lab readings
(docs/plans/EMR_IMPORT_PLAN.md §3.5). Pure `decide()` decisions — the 100%
security-path matrix: corrected supersedes (even same-value), new draw
accumulates, same-instant disagreement pends, preliminary -> final promotes,
cancelled retracts, and a None-status (non-lab) candidate is byte-for-byte the
unchanged measurement path. Re-run idempotency (§6.6) is pinned too.
"""

from datetime import UTC, datetime
from typing import Any

from jbrain.analysis.supersession import Candidate, FactView, decide

DRAW = datetime(2026, 2, 1, 6, 14, tzinfo=UTC)   # the collected instant of one draw
OTHER = datetime(2025, 6, 15, 9, 5, tzinfo=UTC)  # a different draw
PLT = "10*3/uL"


def view(**overrides: Any) -> FactView:
    d: dict[str, Any] = {
        "id": "f-final",
        "kind": "measurement",
        "statement": "Platelet count 9",
        "value_json": {"value": 9, "unit": PLT},
        "object_entity_id": None,
        "assertion": "asserted",
        "valid_from": DRAW,
        "valid_to": None,
        "reported_at": DRAW,
        "status": "active",
        "pinned": False,
    }
    d.update(overrides)
    return FactView(**d)


def cand(**overrides: Any) -> Candidate:
    d: dict[str, Any] = {
        "kind": "measurement",
        "statement": "Platelet count",
        "value_json": {"value": 9, "unit": PLT},
        "object_entity_id": None,
        "assertion": "asserted",
        "valid_from": DRAW,
        "valid_to": None,
        "reported_at": DRAW,
    }
    d.update(overrides)
    return Candidate(**d)


# --- corrected / amended --------------------------------------------------


def test_corrected_different_value_supersedes_prior_final() -> None:
    d = decide(cand(fhir_status="corrected", value_json={"value": 12, "unit": PLT}), [view()])
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == ["f-final"] and not d.hold_ids


def test_corrected_same_value_STILL_transitions_before_idempotency() -> None:
    # The regression that proves _lab_status_transition runs BEFORE the
    # idempotency short-circuit: an identical-value correction must still supersede
    # (so report_status becomes 'corrected'), not silently refresh in place.
    d = decide(cand(fhir_status="corrected", value_json={"value": 9, "unit": PLT}), [view()])
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == ["f-final"]
    assert d.refresh_id is None


def test_amended_collapses_to_the_same_supersede_path() -> None:
    d = decide(cand(fhir_status="amended", value_json={"value": 12, "unit": PLT}), [view()])
    assert d.insert and d.supersede_ids == ["f-final"]


def test_corrected_with_no_prior_behaves_as_first_final() -> None:
    d = decide(cand(fhir_status="corrected"), [])
    assert d.insert and d.insert_status == "active"
    assert not d.supersede_ids and not d.hold_ids


def test_corrected_holds_a_pending_peer() -> None:
    prelim = view(id="f-prelim", status="pending_review")
    d = decide(cand(fhir_status="corrected", value_json={"value": 12, "unit": PLT}), [prelim])
    assert d.insert and d.hold_ids == ["f-prelim"] and not d.supersede_ids


# --- new draw accumulates (the flag must stay meaningful) ------------------


def test_new_draw_accumulates_not_supersedes() -> None:
    older = view(id="f-old", value_json={"value": 212, "unit": PLT}, valid_from=OTHER,
                 reported_at=OTHER)
    d = decide(cand(fhir_status="final", value_json={"value": 9, "unit": PLT}), [older])
    assert d.insert and d.insert_status == "active"
    assert not d.supersede_ids and d.review_kind is None


# --- same-instant disagreement between two finals -> fact_conflict ---------


def test_two_disagreeing_finals_same_instant_conflict() -> None:
    d = decide(cand(fhir_status="final", value_json={"value": 8, "unit": PLT}), [view()])
    assert d.insert and d.insert_status == "pending_review"
    assert d.review_kind == "fact_conflict" and d.conflicting_id == "f-final"


# --- preliminary -> pending, then final promotes --------------------------


def test_preliminary_inserts_pending_review() -> None:
    d = decide(cand(fhir_status="preliminary"), [])
    assert d.insert and d.insert_status == "pending_review"
    assert d.review_kind is None


def test_final_promotes_a_preliminary() -> None:
    prelim = view(id="f-prelim", status="pending_review")
    d = decide(cand(fhir_status="final"), [prelim])
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == ["f-prelim"]


# --- cancelled / entered-in-error -> retracted ----------------------------


def test_cancelled_retracts_and_supersedes_prior() -> None:
    d = decide(cand(fhir_status="cancelled"), [view()])
    assert d.insert and d.insert_status == "retracted"
    assert d.supersede_ids == ["f-final"]


def test_entered_in_error_retracts() -> None:
    d = decide(cand(fhir_status="entered-in-error"), [view()])
    assert d.insert and d.insert_status == "retracted"


# --- the inert / regression guards ---------------------------------------


def test_none_status_is_byte_for_byte_the_unchanged_measurement_path() -> None:
    # A non-lab measurement (fhir_status=None) never enters the transition: a new
    # draw accumulates, and a same-instant identical value refreshes idempotently —
    # exactly the shipped behavior.
    older = view(id="f-old", value_json={"value": 212, "unit": PLT}, valid_from=OTHER,
                 reported_at=OTHER)
    accumulate = decide(cand(value_json={"value": 9, "unit": PLT}), [older])
    assert accumulate.insert and not accumulate.supersede_ids
    refresh = decide(cand(value_json={"value": 9, "unit": PLT}), [view()])
    assert refresh.refresh_id == "f-final" and not refresh.insert


def test_registered_status_makes_no_transition() -> None:
    # Dormant: flows through the unchanged path (accumulate beside a distinct draw).
    older = view(id="f-old", valid_from=OTHER, reported_at=OTHER)
    d = decide(cand(fhir_status="registered", value_json={"value": 5, "unit": PLT}), [older])
    assert d.insert and not d.supersede_ids and d.review_kind is None


# --- re-run idempotency (§6.6) --------------------------------------------


def test_corrected_re_run_is_idempotent() -> None:
    # After a correction: the prior final is superseded, the correction is active.
    # Re-feeding the SAME corrected reading must NOT re-transition — it defers to the
    # idempotency refresh so re-analysis is byte-identical.
    superseded = view(id="f-final", status="superseded")
    corrected = view(id="f-corr", value_json={"value": 12, "unit": PLT})
    d = decide(cand(fhir_status="corrected", value_json={"value": 12, "unit": PLT}),
               [superseded, corrected])
    assert d.refresh_id == "f-corr" and not d.insert and not d.supersede_ids


def test_second_distinct_correction_still_transitions() -> None:
    superseded = view(id="f-final", status="superseded")
    corrected = view(id="f-corr", value_json={"value": 12, "unit": PLT})
    d = decide(cand(fhir_status="corrected", value_json={"value": 7, "unit": PLT}),
               [superseded, corrected])
    assert d.insert and d.supersede_ids == ["f-corr"]


def test_cancelled_re_run_refreshes_the_retracted_row() -> None:
    superseded = view(id="f-final", status="superseded")
    retracted = view(id="f-ret", status="retracted")
    d = decide(cand(fhir_status="cancelled"), [superseded, retracted])
    assert d.refresh_id == "f-ret" and not d.insert


def test_preliminary_re_run_refreshes_the_pending_row() -> None:
    prelim = view(id="f-prelim", status="pending_review")
    d = decide(cand(fhir_status="preliminary"), [prelim])
    assert d.refresh_id == "f-prelim" and not d.insert
