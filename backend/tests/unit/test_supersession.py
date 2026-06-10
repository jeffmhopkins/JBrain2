"""The per-kind supersession matrix (docs/ANALYSIS.md "Fact kinds") as pure
decision tests — all six kinds, pinned overrides, and the validity-vs-capture
time rule."""

from datetime import UTC, datetime
from typing import Any

from jbrain.analysis.supersession import (
    Candidate,
    FactView,
    decide,
    is_functional,
    values_equal,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 3, 1, tzinfo=UTC)
T2 = datetime(2026, 6, 1, tzinfo=UTC)


def view(**overrides: Any) -> FactView:
    defaults: dict[str, Any] = {
        "id": "old-1",
        "kind": "state",
        "statement": "lives at 12 Oak St",
        "value_json": None,
        "object_entity_id": None,
        "assertion": "asserted",
        "valid_from": T0,
        "valid_to": None,
        "reported_at": T0,
        "status": "active",
        "pinned": False,
    }
    defaults.update(overrides)
    return FactView(**defaults)


def cand(**overrides: Any) -> Candidate:
    defaults: dict[str, Any] = {
        "kind": "state",
        "statement": "lives at 99 Pine Ave",
        "value_json": None,
        "object_entity_id": None,
        "assertion": "asserted",
        "valid_from": T1,
        "valid_to": None,
        "reported_at": T1,
    }
    defaults.update(overrides)
    return Candidate(**defaults)


# --- identity refresh (re-extraction idempotency) -------------------------


def test_same_value_refreshes_provenance_without_review() -> None:
    d = decide(cand(statement="lives at 12 Oak St", valid_from=T0, reported_at=T0), [view()])
    assert d.refresh_id == "old-1"
    assert not d.insert and d.review_kind is None


def test_same_value_on_superseded_row_refreshes_only_at_same_validity() -> None:
    superseded = view(status="superseded", valid_from=T0)
    # Re-analysis of the old note (same validity) refreshes in place...
    same = cand(statement="lives at 12 Oak St", valid_from=T0)
    assert decide(same, [superseded]).refresh_id == "old-1"
    # ...but re-asserting the old value with NEW validity is a transition.
    moved_back = cand(statement="lives at 12 Oak St", valid_from=T2, reported_at=T2)
    assert decide(moved_back, [superseded]).insert


# --- event / measurement: accumulate, never auto-supersede ----------------


def test_measurement_accumulates_at_new_instant() -> None:
    old = view(kind="measurement", value_json={"value": 182, "unit": "lb"}, valid_from=T0)
    d = decide(cand(kind="measurement", value_json={"value": 180, "unit": "lb"}), [old])
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == [] and d.review_kind is None


def test_measurement_same_instant_same_value_refreshes() -> None:
    old = view(kind="measurement", value_json={"value": 182, "unit": "lb"}, valid_from=T0)
    d = decide(
        cand(kind="measurement", value_json={"value": 182, "unit": "lb"}, valid_from=T0), [old]
    )
    assert d.refresh_id == "old-1"


def test_measurement_conflict_on_same_instant_goes_to_review() -> None:
    old = view(kind="measurement", value_json={"value": 182, "unit": "lb"}, valid_from=T0)
    d = decide(
        cand(kind="measurement", value_json={"value": 150, "unit": "lb"}, valid_from=T0), [old]
    )
    assert d.insert and d.insert_status == "pending_review"
    assert d.review_kind == "fact_conflict" and d.conflicting_id == "old-1"
    assert d.supersede_ids == []  # never auto-supersede a time series


def test_event_never_supersedes() -> None:
    old = view(kind="event", statement="saw Dr. Patel", valid_from=T0)
    d = decide(cand(kind="event", statement="saw Dr. Patel again", valid_from=T1), [old])
    assert d.insert and d.insert_status == "active" and d.supersede_ids == []


# --- state: newest-wins eagerly, close interval, flag review --------------


def test_state_change_supersedes_and_flags() -> None:
    d = decide(cand(), [view()])
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == ["old-1"]
    assert d.review_kind == "fact_conflict"


def test_state_retrospective_note_does_not_displace_current() -> None:
    """Validity time, never capture time: a note ABOUT 2019 captured today
    lands as closed history."""
    current = view(valid_from=T1, reported_at=T1)
    retro = cand(statement="lived at 3 Elm Rd", valid_from=T0, reported_at=T2)
    d = decide(retro, [current])
    assert d.insert and d.insert_status == "superseded"
    assert d.insert_superseded_by == "old-1"
    assert d.insert_valid_to == T1  # closed at the current interval's start
    assert d.supersede_ids == [] and d.review_kind is None


def test_state_pinned_current_is_reflagged_never_flipped() -> None:
    d = decide(cand(), [view(pinned=True)])
    assert d.insert and d.insert_status == "pending_review"
    assert d.supersede_ids == []
    assert d.review_kind == "fact_conflict" and d.conflicting_id == "old-1"


def test_state_first_value_inserts_active_silently() -> None:
    d = decide(cand(), [])
    assert d.insert and d.insert_status == "active" and d.review_kind is None


# --- attribute: hold both, never auto-supersede ----------------------------


def test_attribute_collision_holds_both_sides() -> None:
    old = view(kind="attribute", statement="born 1980-05-02", valid_from=None)
    d = decide(cand(kind="attribute", statement="born 1981-05-02", valid_from=None), [old])
    assert d.insert and d.insert_status == "pending_review"
    assert d.hold_ids == ["old-1"]
    assert d.review_kind == "attribute_collision"
    assert d.supersede_ids == []


def test_attribute_collision_with_pinned_winner_leaves_it_active() -> None:
    old = view(kind="attribute", statement="born 1980-05-02", valid_from=None, pinned=True)
    d = decide(cand(kind="attribute", statement="born 1981-05-02", valid_from=None), [old])
    assert d.insert_status == "pending_review"
    assert d.hold_ids == []  # the pinned human decision stays active
    assert d.review_kind == "attribute_collision"


# --- preference: newest-wins by reported_at, low-urgency flag ---------------


def test_preference_newest_report_wins_with_low_urgency_flag() -> None:
    old = view(kind="preference", statement="prefers window seats", valid_from=None)
    d = decide(
        cand(kind="preference", statement="prefers aisle seats", valid_from=None, reported_at=T1),
        [old],
    )
    assert d.insert and d.supersede_ids == ["old-1"]
    assert d.review_kind == "fact_conflict" and d.review_extra == {"urgency": "low"}


def test_preference_older_report_lands_as_history() -> None:
    """Compared on reported_at even when validity is absent."""
    old = view(kind="preference", statement="prefers aisle seats", valid_from=None, reported_at=T1)
    d = decide(
        cand(kind="preference", statement="prefers window seats", valid_from=None, reported_at=T0),
        [old],
    )
    assert d.insert_status == "superseded" and d.insert_superseded_by == "old-1"


# --- relationship: accumulate unless functional -----------------------------


def test_relationship_accumulates_by_default() -> None:
    old = view(kind="relationship", object_entity_id="acme", statement="volunteers at Acme")
    d = decide(
        cand(kind="relationship", object_entity_id="globex", statement="volunteers at Globex"),
        [old],
        predicate="memberOf",
    )
    assert d.insert and d.insert_status == "active" and d.supersede_ids == []


def test_functional_relationship_supersedes() -> None:
    old = view(kind="relationship", object_entity_id="acme", statement="works at Acme")
    d = decide(
        cand(kind="relationship", object_entity_id="globex", statement="works at Globex"),
        [old],
        predicate="employer",
    )
    assert d.supersede_ids == ["old-1"] and d.review_kind == "fact_conflict"


def test_functional_predicate_allowlist() -> None:
    assert is_functional("employer")
    assert is_functional("worksFor")
    assert is_functional("spouse")
    assert is_functional("residence")
    assert not is_functional("memberOf")
    assert not is_functional("knows")


# --- value identity ---------------------------------------------------------


def test_values_equal_prefers_structured_payload_over_statement() -> None:
    old = view(kind="measurement", value_json={"value": 182, "unit": "lb"})
    same_value = cand(
        kind="measurement", value_json={"value": 182, "unit": "lb"}, statement="reworded"
    )
    assert values_equal(same_value, old)
    different = cand(kind="measurement", value_json={"value": 150, "unit": "lb"})
    assert not values_equal(different, old)


def test_values_equal_uses_object_entity_for_pure_edges() -> None:
    old = view(kind="relationship", object_entity_id="acme", statement="works at Acme Corp")
    assert values_equal(
        cand(kind="relationship", object_entity_id="acme", statement="employed by Acme"), old
    )
    assert not values_equal(cand(kind="relationship", object_entity_id="globex"), old)


# --- assertion transitions ---------------------------------------------------


def test_values_equal_false_on_assertion_flip() -> None:
    """Same edge, inverted assertion: never an idempotent refresh — the
    refresh path only writes rendering/provenance, never assertion."""
    old = view(object_entity_id="civic", statement="I own a Honda Civic.")
    flipped = cand(
        object_entity_id="civic", assertion="negated", statement="I no longer own the Civic."
    )
    assert not values_equal(flipped, old)


def test_disposal_supersedes_asserted_head_instead_of_refreshing() -> None:
    """Negating a bare owns edge closes the asserted head via the state
    newest-wins branch; the inserted head carries the negated assertion."""
    old = view(object_entity_id="civic", statement="I own a Honda Civic.")
    disposal = cand(
        object_entity_id="civic", assertion="negated", statement="I no longer own the Civic."
    )
    d = decide(disposal, [old], predicate="owns")
    assert d.refresh_id is None
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == ["old-1"]


def test_reassertion_after_negation_supersedes_negated_head() -> None:
    """negated -> asserted on the same edge must not refresh the negated row
    in place (the zombie adv_negation_then_reassert guards against)."""
    old = view(
        kind="relationship",
        object_entity_id="acme",
        assertion="negated",
        statement="Bjorn no longer works at Acme.",
    )
    reassert = cand(
        kind="relationship",
        object_entity_id="acme",
        statement="Bjorn works for Acme again.",
    )
    d = decide(reassert, [old], predicate="worksFor")
    assert d.refresh_id is None
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == ["old-1"]


def test_retracted_rows_are_ignored() -> None:
    old = view(status="retracted")
    d = decide(cand(statement="lives at 12 Oak St", valid_from=T0), [old])
    assert d.insert and d.refresh_id is None
