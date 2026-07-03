"""Unit tests for the weight model (W0/Wave-1 Track C, plan N11).

Pure: deterministic ceilings, self-confidence may only lower, per-kind commit
thresholds.
"""

from jbrain.analysis.weight import (
    DEFAULT_THRESHOLD,
    INFERRED_CEILING,
    INFERRED_OVERWRITE_CEILING,
    ConfidenceSignals,
    assess,
    ceiling,
    commit_status,
    effective_weight,
)


def _sig(surface_attested=True, is_supersede=False):
    return ConfidenceSignals(
        surface_attested=surface_attested,
        is_supersede=is_supersede,
    )


def test_surface_attested_ceils_at_one():
    # Whether the predicate is registry-declared is irrelevant here: the
    # two-tier model (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md §1) carries no
    # unknown-predicate penalty, so attestation alone sets the ceiling.
    assert ceiling(_sig()) == 1.0


def test_inferred_is_capped():
    assert ceiling(_sig(surface_attested=False)) == INFERRED_CEILING


def test_inferred_overwrite_is_capped_hardest():
    c = ceiling(_sig(surface_attested=False, is_supersede=True))
    assert c == INFERRED_OVERWRITE_CEILING


def test_supersede_does_not_cap_a_surface_attested_fact():
    # The hard overwrite cap is only for INFERRED overwrites; a surface-attested
    # supersession (the note literally states the new value) stays at 1.0.
    assert ceiling(_sig(surface_attested=True, is_supersede=True)) == 1.0


def test_self_confidence_can_only_lower_never_raise():
    sig = _sig(surface_attested=False)  # ceiling 0.6
    # A model claiming 0.99 is clamped to the ceiling — it can't buy permissiveness.
    assert effective_weight(0.99, sig) == INFERRED_CEILING
    # A model reporting lower than the ceiling is honored (more caution).
    assert effective_weight(0.3, sig) == 0.3


def test_effective_weight_never_negative():
    # The self-report only applies to inferred facts; a negative one floors at 0.
    assert effective_weight(-5.0, _sig(surface_attested=False)) == 0.0


def test_surface_attested_ignores_low_self_confidence():
    # A literally-stated fact gets its full ceiling even if the model under-rates
    # itself (the note is the authority; self-report is noisy run-to-run).
    assert effective_weight(0.3, _sig()) == 1.0


def test_commit_status_uses_per_kind_threshold():
    # attribute is strict (0.8): 0.75 holds for review, 0.85 commits.
    assert commit_status("attribute", 0.75) == "pending_review"
    assert commit_status("attribute", 0.85) == "active"
    # preference is loose (0.5): 0.6 commits.
    assert commit_status("preference", 0.6) == "active"


def test_commit_status_exact_threshold_commits():
    # The >= boundary: a weight exactly at the threshold commits active.
    assert commit_status("attribute", 0.8) == "active"


def test_self_confidence_above_one_is_clamped_to_ceiling():
    assert effective_weight(1.5, _sig()) == 1.0  # surface-attested ceiling is 1.0


def test_unknown_kind_uses_default_threshold():
    assert commit_status("mystery", DEFAULT_THRESHOLD) == "active"
    assert commit_status("mystery", DEFAULT_THRESHOLD - 0.01) == "pending_review"


def test_inferred_attribute_conflict_routes_to_review():
    # The canonical case: a pronoun-inferred gender that would overwrite an
    # existing value. Inferred + supersede → ceiling 0.4, far below attribute's
    # 0.8 threshold → review, never a silent overwrite.
    weight, status = assess("attribute", 0.99, _sig(surface_attested=False, is_supersede=True))
    assert weight == INFERRED_OVERWRITE_CEILING
    assert status == "pending_review"


def test_surface_attested_fact_commits():
    # Surface-attested → full ceiling (1.0), not bounded by the self-report.
    weight, status = assess("relationship", 0.9, _sig())
    assert weight == 1.0
    assert status == "active"
