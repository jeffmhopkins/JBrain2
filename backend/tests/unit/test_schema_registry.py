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
    assert {"name", "name.legal", "name.nickname", "name.given"} <= names
    # …alongside the type's own predicates.
    assert "birthDate" in names and "spouse" in names


def test_lifecycle_status_enum_is_filled_from_status_values(registry: SchemaRegistry) -> None:
    status = registry.type("appointment").predicate("status")
    assert status is not None
    assert status.value_shape == "enum"
    assert set(status.enum_values) == {"tentative", "confirmed", "cancelled", "occurred"}


def test_functional_and_resolution_config(registry: SchemaRegistry) -> None:
    cfg = registry.resolution_config("person")
    assert "name.legal" in cfg["alias_seeding"]
    assert cfg["display_name"][0] == "name.preferred"
    # spouse and name.legal are functional; birthDate (an attribute) is not.
    assert "spouse" in cfg["functional"]
    assert "birthDate" not in cfg["functional"]


def test_prompt_digest_is_advisory_and_tonally_open_vs_closed(registry: SchemaRegistry) -> None:
    person = registry.prompt_digest("person")  # allow_open_predicates: true
    assert "Person" in person and "name.legal" in person
    assert "coin schema.org-style" in person
    bill = registry.prompt_digest("bill")  # allow_open_predicates: false
    assert "only if truly needed" in bill


def test_render_config_exposes_value_shapes(registry: SchemaRegistry) -> None:
    rc = registry.render_config()
    amount = rc["bill"]["predicates"]["amount"]
    assert amount["value_shape"] == "quantity"
    assert rc["person"]["display_name"][0] == "name.preferred"


def test_validate_value_accepts_well_formed(registry: SchemaRegistry) -> None:
    registry.validate_value("bill", "amount", {"value": 142.1, "unit": "USD"})
    registry.validate_value("appointment", "status", "confirmed")
    registry.validate_value("place", "address", {"addressLocality": "Denver"})
    registry.validate_value("person", "name.legal", "Jeffrey Mark Hopkins")


def test_validate_value_rejects_malformed_shape(registry: SchemaRegistry) -> None:
    with pytest.raises(SchemaError):
        registry.validate_value("bill", "amount", {"value": 10})  # missing unit
    with pytest.raises(SchemaError):
        registry.validate_value("appointment", "status", "rescheduled")  # not in enum
    with pytest.raises(SchemaError):
        registry.validate_value("place", "address", {"unknownKey": "x"})  # not in shape


def test_validate_value_never_gates_an_unknown_predicate(registry: SchemaRegistry) -> None:
    # The one invariant: predicate-name validation never rejects. An undeclared
    # long-tail predicate has no shape to check and is accepted silently.
    registry.validate_value("person", "coffee_order", "oat flat white")


def test_default_defs_dir_points_at_repo_schemas() -> None:
    d = default_defs_dir()
    assert d.name == "schemas" and (d / "_meta.yaml").is_file()


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
