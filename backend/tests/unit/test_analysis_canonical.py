"""Canonical-name projection helpers (pure) and the declared-alias
reconciliation with the registry's canonical name.* family."""

from __future__ import annotations

from jbrain.analysis.canonical import (
    _is_animal_name_fact,
    name_fact_value,
    project_display_name,
)
from jbrain.analysis.entities import declared_alias


def test_animal_name_fact_detected_by_species_key() -> None:
    # The pet decomposition shape carries species; that is the Animal signal.
    assert _is_animal_name_fact("name", {"name": "Ricky", "species": "rat"}) is True
    assert _is_animal_name_fact("name", '{"name": "R", "species": "rat"}') is True  # jsonb as str
    # Not an animal: no species, a non-name predicate, or a plain person name.
    assert _is_animal_name_fact("name", {"name": "Bella"}) is False
    assert _is_animal_name_fact("name.full", {"value": "Celine"}) is False
    assert _is_animal_name_fact("birthDate", {"species": "rat"}) is False


def test_name_fact_value_reads_the_whole_name_family() -> None:
    assert (
        name_fact_value("name.full", {"value": "Celine Kitina Hopkins"}) == "Celine Kitina Hopkins"
    )
    assert name_fact_value("name.family", {"value": "Hopkins"}) == "Hopkins"  # not alias-eligible
    assert name_fact_value("name", {"name": "Bella", "species": "dog"}) == "Bella"
    # A jsonb column handed back as a string is parsed.
    assert name_fact_value("name.given", '{"value": "Celine"}') == "Celine"


def test_name_fact_value_ignores_non_name_and_empty() -> None:
    assert name_fact_value("birthDate", {"value": "1987-07-18"}) is None
    assert name_fact_value("name.full", None) is None
    assert name_fact_value("name.full", {"value": "  "}) is None
    assert name_fact_value("name.full", "not json") is None


def test_projection_follows_precedence() -> None:
    precedence = ("name.preferred", "name.given+name.family", "name.full", "name")
    # Preferred wins outright.
    assert project_display_name(precedence, {"name.preferred": "Sam", "name.full": "C"}) == "Sam"
    # No preferred -> compose given + family.
    assert (
        project_display_name(precedence, {"name.given": "Celine", "name.family": "Hopkins"})
        == "Celine Hopkins"
    )
    # Composite needs BOTH parts; a lone given falls through to full.
    assert (
        project_display_name(precedence, {"name.given": "Celine", "name.full": "Celine K Hopkins"})
        == "Celine K Hopkins"
    )
    # Nothing usable -> no projection (caller keeps the existing name).
    assert project_display_name(precedence, {}) is None


def test_declared_alias_recognizes_canonical_name_family() -> None:
    # The slice-1 normalizer emits name.full/name.nickname; declared aliasing
    # must still fire on them (legacy spellings keep working too).
    assert declared_alias("name.full", {"value": "Jeffrey Mark Hopkins"}) == "Jeffrey Mark Hopkins"
    assert declared_alias("name.nickname", {"value": "Dad"}) == "Dad"
    assert declared_alias("legalName", {"value": "Jeffrey Mark Hopkins"}) == "Jeffrey Mark Hopkins"
    # Parity with the legacy set: a bare surname is not a self-declared identity.
    assert declared_alias("name.family", {"value": "Hopkins"}) is None
    # Non-naming predicates are still ignored.
    assert declared_alias("worksFor", {"value": "Acme"}) is None
