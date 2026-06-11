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
    # Both drift spellings of the legal name converge on the canonical address.
    assert plan["legalName"] == "name.legal"
    assert plan["legal_name"] == "name.legal"
    assert plan["alsoKnownAs"] == "name.nickname"
    # Already-canonical and long-tail predicates are not in the plan.
    assert "name.legal" not in plan
    assert "worksFor" not in plan
    assert "contact" not in plan


def test_plan_renames_empty_when_clean() -> None:
    assert plan_renames({"name.legal", "spouse", "birthDate"}, get_registry()) == {}
