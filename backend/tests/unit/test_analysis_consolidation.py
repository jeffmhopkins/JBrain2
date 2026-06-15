"""The consolidation rename planner: drift spellings map to their canonical
predicate, already-canonical and long-tail predicates are left untouched."""

from __future__ import annotations

from jbrain.analysis.consolidation import plan_renames
from jbrain.schema import get_registry


def test_plan_renames_targets_only_drift() -> None:
    plan = plan_renames(
        {"legalName", "legal_name", "name.legal", "alsoKnownAs", "worksFor", "contact"},
        get_registry(),
    )
    # Drift spellings of the full name — including the old name.legal address —
    # converge on the canonical name.full.
    assert plan["legalName"] == "name.full"
    assert plan["legal_name"] == "name.full"
    assert plan["name.legal"] == "name.full"
    assert plan["alsoKnownAs"] == "name.nickname"
    # Already-canonical and long-tail predicates are not in the plan.
    assert "name.full" not in plan
    assert "worksFor" not in plan
    assert "contact" not in plan


def test_plan_renames_empty_when_clean() -> None:
    assert plan_renames({"name.full", "spouse", "birthDate"}, get_registry()) == {}
