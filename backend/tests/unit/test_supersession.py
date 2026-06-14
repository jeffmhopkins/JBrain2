"""The per-kind supersession matrix (docs/ANALYSIS.md "Fact kinds") as pure
decision tests — all six kinds, pinned overrides, and the validity-vs-capture
time rule."""

from datetime import UTC, datetime
from typing import Any

from jbrain.analysis.supersession import (
    Candidate,
    FactView,
    decide,
    inverse_predicate,
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


# --- confidence guard: low-confidence never auto-supersedes ------------------


def test_low_confidence_candidate_never_auto_supersedes() -> None:
    """The H2 guard: a blurry OCR read (0.25) parks in pending_review behind
    a low_confidence card; the confident prior stays active."""
    confident = view(value_json={"drug": "lisinopril"}, confidence=0.95)
    ocr = cand(value_json={"drug": "losartan"}, confidence=0.25)
    d = decide(ocr, [confident])
    assert d.insert and d.insert_status == "pending_review"
    assert d.review_kind == "low_confidence" and d.conflicting_id == "old-1"
    assert d.supersede_ids == [] and d.hold_ids == []


def test_confidence_at_threshold_supersedes_normally() -> None:
    """LOW_CONFIDENCE is exclusive: exactly 0.5 is not 'low'."""
    d = decide(cand(confidence=0.5), [view(confidence=0.95)])
    assert d.supersede_ids == ["old-1"] and d.review_kind == "fact_conflict"


def test_low_confidence_may_replace_an_even_shakier_fact() -> None:
    """The guard protects HIGHER-confidence knowledge only; between two weak
    facts, newest still wins (with the usual conflict flag)."""
    d = decide(cand(confidence=0.4), [view(confidence=0.2)])
    assert d.supersede_ids == ["old-1"] and d.review_kind == "fact_conflict"


# --- in-place interval close ------------------------------------------------


def test_end_date_backfill_closes_open_edge_in_place() -> None:
    """'Left Acme back in March': same object + valid_from, new valid_to —
    close the one open row; no duplicate, no chain, no conflict."""
    old = view(kind="relationship", object_entity_id="acme", value_json={"state": "current"})
    d = decide(
        cand(
            kind="relationship",
            object_entity_id="acme",
            value_json={"state": "ended", "end": "2026-03"},
            valid_from=T0,
            valid_to=T1,
            reported_at=T2,
        ),
        [old],
        predicate="employer",
    )
    assert d.close_id == "old-1" and d.close_valid_to == T1
    assert not d.insert and d.refresh_id is None and d.review_kind is None


def test_end_date_backfill_closes_scalar_state_instead_of_refreshing() -> None:
    """values_equal + a new valid_to must hit the close path, not the refresh
    path (refresh only writes rendering/provenance and would drop the end)."""
    d = decide(
        cand(statement="lives at 12 Oak St", valid_from=T0, valid_to=T1, reported_at=T2),
        [view()],
    )
    assert d.close_id == "old-1" and d.close_valid_to == T1


def test_close_requires_open_interval_and_matching_start() -> None:
    already_closed = view(valid_to=T1)
    d = decide(
        cand(statement="lives at 12 Oak St", valid_from=T0, valid_to=T2, reported_at=T2),
        [already_closed],
    )
    assert d.close_id is None
    other_start = cand(statement="lives at 12 Oak St", valid_from=T1, valid_to=T2, reported_at=T2)
    assert decide(other_start, [view()]).close_id is None


def test_close_requires_same_assertion() -> None:
    """A disposal ('no longer own X') flips the assertion: that is a state
    TRANSITION (supersede with a negated head), never an in-place close."""
    old = view(object_entity_id="civic", statement="I own a Honda Civic.")
    disposal = cand(
        object_entity_id="civic",
        assertion="negated",
        statement="I no longer own the Civic.",
        valid_from=T0,
        valid_to=T1,
    )
    d = decide(disposal, [old], predicate="owns")
    assert d.close_id is None
    assert d.insert and d.supersede_ids == ["old-1"]


def test_close_never_edits_pinned_row() -> None:
    d = decide(
        cand(statement="lives at 12 Oak St", valid_from=T0, valid_to=T1, reported_at=T1),
        [view(pinned=True)],
    )
    assert d.close_id is None  # refresh may touch rendering, never the interval
    assert d.refresh_id == "old-1"


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


# --- schedule bindings: newest INSTRUCTION wins, either direction -----------


def test_reschedule_earlier_still_supersedes() -> None:
    """The binding's value IS a validity instant, so validity ordering would
    let the stale later time win; the newest reported instruction must win."""
    friday = view(value_json={"start": "2026-06-19T14:00"}, valid_from=T1, reported_at=T0)
    wednesday = cand(value_json={"start": "2026-06-17T14:00"}, valid_from=T0, reported_at=T1)
    d = decide(wednesday, [friday], predicate="scheduledTime")
    assert d.insert and d.insert_status == "active"
    assert d.supersede_ids == ["old-1"]
    assert d.review_kind == "fact_conflict"


def test_reschedule_later_supersedes_unchanged() -> None:
    friday = view(value_json={"start": "2026-06-19T14:00"}, valid_from=T0, reported_at=T0)
    monday = cand(value_json={"start": "2026-06-22T14:00"}, valid_from=T1, reported_at=T1)
    d = decide(monday, [friday], predicate="scheduledTime")
    assert d.supersede_ids == ["old-1"]


def test_stale_schedule_instruction_lands_as_history() -> None:
    """Out-of-order outbox: an OLDER instruction about the same binding must
    not displace the newer one, whatever times the two carry."""
    current = view(value_json={"start": "2026-06-17T14:00"}, valid_from=T0, reported_at=T2)
    stale = cand(value_json={"start": "2026-06-19T14:00"}, valid_from=T1, reported_at=T1)
    d = decide(stale, [current], predicate="scheduledTime")
    assert d.insert_status == "superseded" and d.insert_superseded_by == "old-1"


def test_ordinary_state_keeps_validity_ordering() -> None:
    """homeLocation et al are untouched by the schedule rule: a later-reported
    note about an EARLIER validity still lands as history."""
    current = view(valid_from=T1, reported_at=T1)
    retro = cand(statement="lived at 3 Elm Rd", valid_from=T0, reported_at=T2)
    d = decide(retro, [current], predicate="homeLocation")
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
    # Residual hardcoded set (role-edge concepts the registry doesn't name as
    # bare predicates) — behavior preserved.
    assert is_functional("employer")
    assert is_functional("worksFor")
    assert is_functional("residence")
    # Registry-driven half: the schema's `functional` flag now governs too.
    assert is_functional("spouse")  # both sources
    assert is_functional("location")  # appointment.location — registry only (new)
    assert is_functional("organizer")  # appointment.organizer — registry only (new)
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


# --- unit-normalized value identity ------------------------------------------


def test_unit_change_same_measurement_is_equal() -> None:
    """180 lb restated as 81.6 kg (rounded) is the SAME reading: refresh at
    the same instant, never a fact_conflict."""
    old = view(kind="measurement", value_json={"value": 180, "unit": "lb"}, valid_from=T0)
    metric = cand(kind="measurement", value_json={"value": 81.6, "unit": "kg"}, valid_from=T0)
    assert values_equal(metric, old)
    assert decide(metric, [old]).refresh_id == "old-1"


def test_unit_change_different_value_still_conflicts() -> None:
    old = view(kind="measurement", value_json={"value": 180, "unit": "lb"}, valid_from=T0)
    lighter = cand(kind="measurement", value_json={"value": 75, "unit": "kg"}, valid_from=T0)
    assert not values_equal(lighter, old)
    d = decide(lighter, [old])
    assert d.insert_status == "pending_review" and d.review_kind == "fact_conflict"


def test_unit_equivalence_for_length_and_temperature() -> None:
    height = view(kind="measurement", value_json={"value": 70, "unit": "in"}, valid_from=T0)
    assert values_equal(
        cand(kind="measurement", value_json={"value": 177.8, "unit": "cm"}, valid_from=T0), height
    )
    fever = view(kind="measurement", value_json={"value": 98.6, "unit": "°F"}, valid_from=T0)
    assert values_equal(
        cand(kind="measurement", value_json={"value": 37, "unit": "°C"}, valid_from=T0), fever
    )


def test_unit_epsilon_boundary() -> None:
    """Rounding to ~3 significant figures is equal; a whole-unit re-round
    (180 lb -> '82 kg') is outside tolerance and keeps the conflict path."""
    old = view(kind="measurement", value_json={"value": 180, "unit": "lb"}, valid_from=T0)
    assert values_equal(
        cand(kind="measurement", value_json={"value": 81.65, "unit": "kg"}, valid_from=T0), old
    )
    assert not values_equal(
        cand(kind="measurement", value_json={"value": 82, "unit": "kg"}, valid_from=T0), old
    )


def test_non_convertible_units_fall_through_to_conflict() -> None:
    old = view(kind="measurement", value_json={"value": 1100, "unit": "steps"}, valid_from=T0)
    other = cand(kind="measurement", value_json={"value": 1.1, "unit": "ksteps"}, valid_from=T0)
    assert not values_equal(other, old)


def test_extra_value_json_keys_must_match_for_unit_equality() -> None:
    left_arm = view(
        kind="measurement", value_json={"value": 180, "unit": "lb", "site": "home"}, valid_from=T0
    )
    bare = cand(kind="measurement", value_json={"value": 81.6, "unit": "kg"}, valid_from=T0)
    assert not values_equal(bare, left_arm)


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


# --- inverse-predicate registry (mutual/inverse edges, Issue 2) ------------


def test_symmetric_predicate_reflects_with_same_predicate() -> None:
    # Same predicate back, in the spelling the source used.
    assert inverse_predicate("spouse") == "spouse"
    assert inverse_predicate("sibling_of") == "sibling_of"
    assert inverse_predicate("co_founder") == "co_founder"
    # Twins are siblings; a bare `twin` predicate reciprocates symmetrically too,
    # so "Lydian and Elora are twins" mirrors onto Elora's stream.
    assert inverse_predicate("sibling") == "sibling"
    assert inverse_predicate("twin") == "twin"
    # The gender-neutral romantic predicate reflects with itself, the safe
    # default the gendered boyfriend/girlfriend pair can't be.
    assert inverse_predicate("partner") == "partner"
    assert inverse_predicate("significant_other") == "significant_other"


def test_kinship_parent_child_reflect_with_named_inverse() -> None:
    # The four-daughters fix: the kinship predicates the prompt steers toward
    # reciprocate, so a parent's `children` edge mirrors to the child's `parent`
    # edge (and a bare `child` reads the same direction as `children`).
    assert inverse_predicate("children") == "parent"
    assert inverse_predicate("parent") == "children"
    assert inverse_predicate("child") == "parent"


def test_asymmetric_predicate_reflects_with_named_inverse() -> None:
    assert inverse_predicate("worksFor") == "employs"
    assert inverse_predicate("employs") == "worksFor"
    assert inverse_predicate("parent_of") == "child_of"
    assert inverse_predicate("child_of") == "parent_of"
    assert inverse_predicate("manages") == "reportsTo"
    assert inverse_predicate("hasTreated") == "treatedBy"
    # Dating reciprocates with the opposite gendered word, so a `boyfriend` edge
    # mirrors to a `girlfriend` edge on the other party's stream (and back).
    assert inverse_predicate("boyfriend") == "girlfriend"
    assert inverse_predicate("girlfriend") == "boyfriend"
    # Ownership reciprocates owner <-> possession (me.owns -> F-150 reflects to
    # F-150.ownedBy -> me on the thing's stream).
    assert inverse_predicate("owns") == "ownedBy"
    assert inverse_predicate("ownedBy") == "owns"
    # Membership: only the unambiguous person->org spelling reciprocates, onto
    # the org's member list. A bare `member` is directionally ambiguous and
    # stands alone (no wrong-way edge).
    assert inverse_predicate("memberOf") == "member"
    assert inverse_predicate("member") is None


def test_unknown_predicate_has_no_inverse() -> None:
    # The registry is an allowlist; an unknown relation stands alone (safe).
    assert inverse_predicate("favoriteBand") is None
    assert inverse_predicate("admires") is None


def test_inverse_lookup_is_case_insensitive() -> None:
    assert inverse_predicate("WORKSFOR") == "employs"
    assert inverse_predicate("Spouse") == "Spouse"


# --- derived-defers-to-primary decision shaping ----------------------------
#
# The pipeline reads the derived flag off existing rows; these pin the
# decide() shapes it relies on. A symmetric inverse runs decide() on the
# object's own key, so an existing PRIMARY head there must produce a
# supersession the pipeline can recognize and demote to a conflict (it never
# auto-overwrites a primary with a reflection), while a DERIVED head may be
# freely superseded.


def test_derived_inverse_supersedes_existing_head_on_functional_key() -> None:
    # Celine already has a spouse edge; a newer derived candidate supersedes
    # on the functional key — the pipeline inspects supersede_ids' derived
    # flags to decide whether to honor or demote this.
    existing = view(
        kind="relationship",
        object_entity_id="old-jeff",
        statement="Celine's spouse is OldJeff.",
        valid_from=T0,
    )
    candidate = cand(
        kind="relationship",
        object_entity_id="jeff",
        statement="Celine's spouse is Jeff.",
        valid_from=T1,
    )
    d = decide(candidate, [existing], predicate="spouse")
    assert d.supersede_ids == ["old-1"]


def test_non_functional_inverse_accumulates() -> None:
    # employs is not functional: a second inbound edge just inserts.
    existing = view(
        kind="relationship",
        object_entity_id="alice",
        statement="Globex employs Alice.",
    )
    candidate = cand(
        kind="relationship",
        object_entity_id="marcus",
        statement="Globex employs Marcus.",
    )
    d = decide(candidate, [existing], predicate="employs")
    assert d.insert and not d.supersede_ids
