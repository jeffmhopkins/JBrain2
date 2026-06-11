"""Canonical-name projection helpers (pure) and the declared-alias
reconciliation with the registry's canonical name.* family."""

from __future__ import annotations

from jbrain.analysis.canonical import name_fact_value, project_display_name
from jbrain.analysis.entities import declared_alias


def test_name_fact_value_reads_the_whole_name_family() -> None:
    assert (
        name_fact_value("name.legal", {"value": "Celine Kitina Hopkins"}) == "Celine Kitina Hopkins"
    )
    assert name_fact_value("name.family", {"value": "Hopkins"}) == "Hopkins"  # not alias-eligible
    assert name_fact_value("name", {"name": "Bella", "species": "dog"}) == "Bella"
    # A jsonb column handed back as a string is parsed.
    assert name_fact_value("name.given", '{"value": "Celine"}') == "Celine"


def test_name_fact_value_ignores_non_name_and_empty() -> None:
    assert name_fact_value("birthDate", {"value": "1987-07-18"}) is None
    assert name_fact_value("name.legal", None) is None
    assert name_fact_value("name.legal", {"value": "  "}) is None
    assert name_fact_value("name.legal", "not json") is None


def test_projection_follows_precedence() -> None:
    precedence = ("name.preferred", "name.given+name.family", "name.legal", "name")
    # Preferred wins outright.
    assert project_display_name(precedence, {"name.preferred": "Sam", "name.legal": "C"}) == "Sam"
    # No preferred -> compose given + family.
    assert (
        project_display_name(precedence, {"name.given": "Celine", "name.family": "Hopkins"})
        == "Celine Hopkins"
    )
    # Composite needs BOTH parts; a lone given falls through to legal.
    assert (
        project_display_name(precedence, {"name.given": "Celine", "name.legal": "Celine K Hopkins"})
        == "Celine K Hopkins"
    )
    # Nothing usable -> no projection (caller keeps the existing name).
    assert project_display_name(precedence, {}) is None


def test_declared_alias_recognizes_canonical_name_family() -> None:
    # The slice-1 normalizer emits name.legal/name.nickname; declared aliasing
    # must still fire on them (legacy spellings keep working too).
    assert declared_alias("name.legal", {"value": "Jeffrey Mark Hopkins"}) == "Jeffrey Mark Hopkins"
    assert declared_alias("name.nickname", {"value": "Dad"}) == "Dad"
    assert declared_alias("legalName", {"value": "Jeffrey Mark Hopkins"}) == "Jeffrey Mark Hopkins"
    # Parity with the legacy set: a bare surname is not a self-declared identity.
    assert declared_alias("name.family", {"value": "Hopkins"}) is None
    # Non-naming predicates are still ignored.
    assert declared_alias("worksFor", {"value": "Acme"}) is None
