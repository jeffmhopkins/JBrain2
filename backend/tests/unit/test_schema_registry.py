"""The entity schema registry: the shipped `schemas/` load and validate, the
roll-down composes facets, the projections are coherent, and the value-shape
validator rejects malformed values WITHOUT ever gating a predicate name
(docs/entity.md invariant). Malformed definitions fail at load (SchemaError)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jbrain.schema import SchemaError, SchemaRegistry, load_registry
from jbrain.schema.loader import default_defs_dir


@pytest.fixture(scope="module")
def registry() -> SchemaRegistry:
    return load_registry()


def test_shipped_registry_loads_all_fourteen_types(registry: SchemaRegistry) -> None:
    expected = {
        "person",
        "organization",
        "place",
        "role",
        "animal",
        "appointment",
        "bill",
        "lab_result",
        "vehicle",
        "medication",
        "financial_account",
        "document",
        "subscription",
        "device",
    }
    assert set(registry.types) == expected
    assert registry.meta.schema_version >= 1


def test_facets_roll_down_into_types(registry: SchemaRegistry) -> None:
    person = registry.type("person")
    names = {p.canonical_name for p in person.effective_predicates}
    # From the Named facet…
    assert {"name", "name.full", "name.nickname", "name.given"} <= names
    # …alongside the type's own predicates.
    assert "birthDate" in names and "spouse" in names


def test_lifecycle_status_enum_is_filled_from_status_values(registry: SchemaRegistry) -> None:
    status = registry.type("appointment").predicate("status")
    assert status is not None
    assert status.value_shape == "enum"
    assert set(status.enum_values) == {"tentative", "confirmed", "cancelled", "occurred"}


def test_person_carries_display_precedence_and_alias_seeding(registry: SchemaRegistry) -> None:
    # The schema data future projections will consume (kept + loader-validated
    # even though the projection methods aren't built): the display-name
    # precedence and the alias-seeding predicates roll down onto the type.
    person = registry.type("person")
    assert person.display_name[0] == "name.preferred"
    assert "name.full" in person.alias_seeding_predicates


def test_default_defs_dir_points_at_packaged_defs() -> None:
    d = default_defs_dir()
    assert d.name == "defs" and (d / "_meta.yaml").is_file()


def test_predicate_normalization_collapses_drift_spellings(registry: SchemaRegistry) -> None:
    # The screenshot drift: legalName / legal_name / "Legal Name" all converge —
    # onto name.full now (a stated full/legal name is the formal name, not a
    # claim it is the registered legal one). The old name.legal address folds too.
    assert registry.normalize_predicate("legalName") == "name.full"
    assert registry.normalize_predicate("legal_name") == "name.full"
    assert registry.normalize_predicate("Legal Name") == "name.full"
    assert registry.normalize_predicate("name.legal") == "name.full"
    assert registry.normalize_predicate("alsoKnownAs") == "name.nickname"
    assert registry.normalize_predicate("scheduled_time") == "scheduledTime"
    # An already-canonical or long-tail predicate passes through untouched.
    assert registry.normalize_predicate("name.full") == "name.full"
    assert registry.normalize_predicate("coffee_order") == "coffee_order"


def test_is_functional_reads_the_registry_flag(registry: SchemaRegistry) -> None:
    # Functional predicates the schema declares (any-type union), via canonical
    # and drift spellings — and a non-functional relationship stays accumulating.
    assert registry.is_functional("spouse")
    assert registry.is_functional("location")  # appointment.location (relationship)
    assert registry.is_functional("organizer")  # appointment.organizer (relationship)
    assert registry.is_functional("scheduled_time")  # drift -> scheduledTime (functional)
    assert not registry.is_functional("knows")
    assert not registry.is_functional("coffee_order")


def test_predicate_for_kind_resolves_by_id_and_schema_org_name(registry: SchemaRegistry) -> None:
    # entities.kind may be the schema.org name ("Person") or the type id ("person").
    assert registry.predicate_for_kind("Person", "spouse") is not None
    assert registry.predicate_for_kind("person", "spouse") is not None
    # Normalizes the drift spelling before lookup.
    assert registry.predicate_for_kind("Person", "legalName") is not None
    # Unknown kind or undeclared predicate -> None (never a storage gate).
    assert registry.predicate_for_kind("Nonsense", "spouse") is None
    assert registry.predicate_for_kind("Person", "coffee_order") is None


def test_declares_predicate_is_kind_agnostic(registry: SchemaRegistry) -> None:
    # Cheap membership across all types (canonical + drift), no kind needed.
    assert registry.declares_predicate("spouse")
    assert registry.declares_predicate("legalName")  # drift -> name.full
    assert not registry.declares_predicate("coffee_order")


def test_validate_value_is_conservative(registry: SchemaRegistry) -> None:
    spouse = registry.predicate_for_kind("Person", "spouse")  # value_shape: ref
    full = registry.predicate_for_kind("Person", "name.full")  # value_shape: text
    assert spouse is not None and full is not None
    # None always passes (the datum lives in the statement / temporal token).
    assert registry.validate_value(spouse, None, object_present=True)
    # ref: an edge needs an object, a scalar payload with no object is the violation.
    assert registry.validate_value(spouse, {"value": "Jane"}, object_present=True)
    assert not registry.validate_value(spouse, {"value": "Jane"}, object_present=False)
    # text/scalar never reject (value_fidelity lives in the statement).
    assert registry.validate_value(full, {"value": "Celine Kitina Hopkins"}, object_present=False)


def test_validate_value_enum_and_structured(registry: SchemaRegistry) -> None:
    from jbrain.schema.models import Predicate

    reg = registry
    addr = Predicate(
        canonical_name="address", value_shape="structured", kind="state", shape="postal_address"
    )
    assert reg.validate_value(addr, {"addressLocality": "Portland"}, object_present=False)
    assert not reg.validate_value(addr, {"bogusKey": "x"}, object_present=False)
    enum = Predicate(
        canonical_name="status", value_shape="enum", kind="state", enum_values=("active", "closed")
    )
    assert reg.validate_value(enum, {"value": "active"}, object_present=False)
    assert not reg.validate_value(enum, {"value": "frobnicated"}, object_present=False)
    # quantity tolerates multi-field measurements; only a non-dict is a violation.
    qty = Predicate(canonical_name="bp", value_shape="quantity", kind="measurement")
    assert reg.validate_value(
        qty, {"systolic": 120, "diastolic": 80, "unit": "mmHg"}, object_present=False
    )


def test_conflicting_renamed_from_fails_to_load(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    # Two predicates both claim the alias "aka" — an unresolvable attractor.
    (tmp_path / "facets.yaml").write_text(
        yaml.safe_dump(
            {
                "facets": {
                    "Named": {
                        "predicates": [
                            {
                                "canonical_name": "name",
                                "value_shape": "text",
                                "kind": "attribute",
                                "renamed_from": ["aka"],
                            },
                            {
                                "canonical_name": "name.legal",
                                "value_shape": "text",
                                "kind": "state",
                                "renamed_from": ["aka"],
                            },
                        ]
                    }
                }
            }
        )
    )
    with pytest.raises(SchemaError, match="maps to both"):
        load_registry(tmp_path)


# --- malformed definitions fail at load -------------------------------------


def _write_min_registry(root: Path) -> None:
    """A minimal valid registry under `root`, for negative tests to corrupt."""
    (root / "types").mkdir(parents=True)
    (root / "_meta.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "fact_kinds": ["attribute", "state"],
                "value_shapes": ["text", "enum", "ref"],
                "shapes": {},
                "tones": ["x"],
            }
        )
    )
    (root / "facets.yaml").write_text(
        yaml.safe_dump(
            {
                "facets": {
                    "Named": {
                        "description": "",
                        "predicates": [
                            {"canonical_name": "name", "value_shape": "text", "kind": "attribute"}
                        ],
                    }
                }
            }
        )
    )
    (root / "types" / "person.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "person",
                "name": "Person",
                "facets": ["Named"],
                "default_fact_kind": "attribute",
                "display_name": ["name"],
            }
        )
    )


def test_minimal_registry_round_trips(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    reg = load_registry(tmp_path)
    assert reg.type("person").predicate("name") is not None


def test_unknown_facet_reference_fails(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    (tmp_path / "types" / "person.yaml").write_text(
        yaml.safe_dump(
            {"id": "person", "name": "Person", "facets": ["Ghost"], "display_name": ["name"]}
        )
    )
    with pytest.raises(SchemaError, match="unknown facet"):
        load_registry(tmp_path)


def test_unknown_value_shape_fails(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    (tmp_path / "types" / "bad.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "bad",
                "name": "Bad",
                "default_fact_kind": "attribute",
                "predicates": [{"canonical_name": "x", "value_shape": "blob"}],
                "display_name": ["x"],
            }
        )
    )
    with pytest.raises(SchemaError, match="unknown value_shape"):
        load_registry(tmp_path)


def test_enum_without_values_fails(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    (tmp_path / "types" / "bad.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "bad",
                "name": "Bad",
                "default_fact_kind": "attribute",
                "predicates": [{"canonical_name": "x", "value_shape": "enum"}],
                "display_name": ["x"],
            }
        )
    )
    with pytest.raises(SchemaError, match="no enum_values"):
        load_registry(tmp_path)


def test_ref_to_unknown_type_fails(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    (tmp_path / "types" / "bad.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "bad",
                "name": "Bad",
                "default_fact_kind": "attribute",
                "predicates": [{"canonical_name": "x", "value_shape": "ref", "range_type": "nope"}],
                "display_name": ["x"],
            }
        )
    )
    with pytest.raises(SchemaError, match="unknown type"):
        load_registry(tmp_path)


def test_alias_seed_must_be_a_property(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    (tmp_path / "types" / "person.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "person",
                "name": "Person",
                "facets": ["Named"],
                "alias_seeding_predicates": ["name.legal"],  # not declared anywhere
                "display_name": ["name"],
            }
        )
    )
    with pytest.raises(SchemaError, match="alias_seeding"):
        load_registry(tmp_path)


def test_missing_display_name_fails(tmp_path: Path) -> None:
    _write_min_registry(tmp_path)
    (tmp_path / "types" / "person.yaml").write_text(
        yaml.safe_dump({"id": "person", "name": "Person", "facets": ["Named"]})
    )
    with pytest.raises(SchemaError, match="display_name"):
        load_registry(tmp_path)
