"""Pure-logic entity resolution helpers: reference-shape parsing, role
predicate matching, and the layer-3 disambiguation contract. The session-bound
layers are proven against real Postgres in
tests/integration/test_entity_resolution_pg.py."""

import json

from jbrain.analysis.entities import (
    Reference,
    build_disambiguation_prompt,
    kind_hint_compatible,
    normalize_alias,
    parse_disambiguation,
    parse_reference,
    predicate_denotes_role,
)


class TestParseReference:
    def test_role(self) -> None:
        assert parse_reference("my dentist") == Reference(shape="role", owner=None, noun="dentist")

    def test_role_multiword_and_case(self) -> None:
        assert parse_reference("My primary care doctor") == Reference(
            shape="role", owner=None, noun="primary care doctor"
        )

    def test_definite(self) -> None:
        assert parse_reference("the rat") == Reference(shape="definite", owner=None, noun="rat")

    def test_possessive(self) -> None:
        assert parse_reference("Summer's rat") == Reference(
            shape="possessive", owner="Summer", noun="rat"
        )

    def test_possessive_curly_apostrophe(self) -> None:
        assert parse_reference("Summer’s rat") == Reference(
            shape="possessive", owner="Summer", noun="rat"
        )

    def test_plain_name_is_not_a_reference(self) -> None:
        assert parse_reference("Sarah") is None
        assert parse_reference("Dr. Okafor") is None

    def test_possessive_looking_proper_name_still_parses(self) -> None:
        # Tolerated by design: the hop only links when the owner resolves AND
        # owns a match, so "Bob's Burgers" falls through to plain creation.
        assert parse_reference("Bob's Burgers") == Reference(
            shape="possessive", owner="Bob", noun="Burgers"
        )

    def test_bare_articles_are_not_references(self) -> None:
        assert parse_reference("Theo") is None
        assert parse_reference("Myra") is None

    def test_normalized_name_with_reference_surface(self) -> None:
        # Live models normalize "the rat" to the invented name "Rat"; the
        # verbatim surface_text is what still carries the reference shape, so
        # the resolver re-parses it when the name reads as a plain name.
        assert parse_reference("Rat") is None
        assert parse_reference("The rat") == Reference(shape="definite", owner=None, noun="rat")
        assert parse_reference("My dog") == Reference(shape="role", owner=None, noun="dog")


class TestKindHintCompatible:
    def test_generic_and_empty_hints_pass(self) -> None:
        assert kind_hint_compatible("", "pet", "rat")
        assert kind_hint_compatible("Thing", "Organization", "bank")

    def test_equality_case_insensitive(self) -> None:
        assert kind_hint_compatible("Animal", "animal", "rat")
        assert kind_hint_compatible("Organization", "organization", "bank")

    def test_creature_vocabulary_tolerated(self) -> None:
        # Live extractions coin "pet", "animal", or the species for the same
        # creature; the hint filter must not hide the one real candidate.
        assert kind_hint_compatible("animal", "pet", "rat")
        assert kind_hint_compatible("pet", "animal", "rat")
        assert kind_hint_compatible("animal", "rat", "rat")
        assert kind_hint_compatible("animal", "rats", "rat")

    def test_non_creature_kinds_still_require_equality(self) -> None:
        assert not kind_hint_compatible("Organization", "Place", "bank")
        assert not kind_hint_compatible("animal", "Person", "rat")


class TestPredicateDenotesRole:
    def test_snake_case(self) -> None:
        assert predicate_denotes_role("dentist_of", "dentist")

    def test_camel_case(self) -> None:
        assert predicate_denotes_role("dentistOf", "dentist")

    def test_exact(self) -> None:
        assert predicate_denotes_role("employer", "employer")

    def test_synonyms_do_not_match(self) -> None:
        # "my boss" vs an `employer` fact: a miss means review, never a guess.
        assert not predicate_denotes_role("employer", "boss")

    def test_unrelated(self) -> None:
        assert not predicate_denotes_role("owns", "dentist")


class TestDisambiguationContract:
    def test_prompt_carries_candidates_verbatim(self) -> None:
        items = [
            {
                "name": "Bob Smith",
                "kind": "Person",
                "context": "Bob Smith called.",
                "candidates": [{"id": "abc-123", "name": "Robert Smith", "kind": "Person"}],
            }
        ]
        prompt = build_disambiguation_prompt(items)
        assert json.loads(prompt) == {"mentions": items}

    def test_parse_choice_and_none(self) -> None:
        parsed = {
            "choices": [
                {"name": "Bob Smith", "entity_id": "abc-123"},
                {"name": "Acme", "entity_id": None},
            ]
        }
        assert parse_disambiguation(parsed) == {"Bob Smith": "abc-123", "Acme": None}

    def test_malformed_payloads_yield_nothing(self) -> None:
        # Unanswered = review inbox; a junk reply must not become links.
        assert parse_disambiguation(None) == {}
        assert parse_disambiguation({"choices": "nope"}) == {}
        assert parse_disambiguation({"choices": [{"entity_id": "x"}, 7]}) == {}

    def test_non_string_entity_id_reads_as_none(self) -> None:
        assert parse_disambiguation({"choices": [{"name": "X", "entity_id": 42}]}) == {"X": None}


def test_normalize_alias_strips_case_diacritics_whitespace() -> None:
    assert normalize_alias("  Dr.  Okafor ") == "dr. okafor"
    assert normalize_alias("Zoë") == "zoe"
