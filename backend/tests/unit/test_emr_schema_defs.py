"""EMR-import schema defs: activation of lab_result, the new encounter /
medical_condition types, and the predicate-registry collision audit (§3.4).

Pure — the real registry is loaded from YAML, no DB. These pin the Wave-1
storage-vocabulary decisions so a later edit can't silently reintroduce the
Lifecycle status, collapse a shape-divergent name in the global seed, or drop
one of the audited encounter predicates.
"""

from __future__ import annotations

from jbrain.analysis.predicates import registry_seed_rows
from jbrain.schema.loader import load_registry

_REG = load_registry()
_SEEDS = {s.canonical_name: s for s in registry_seed_rows(_REG)}


def _eff(tid: str) -> dict[str, tuple[str, str, bool]]:
    return {
        p.canonical_name: (p.value_shape, p.kind, p.functional)
        for p in _REG.type(tid).effective_predicates
    }


# --- lab_result activation (§3.2) ----------------------------------------


def test_lab_result_active_lifecycle_dropped_category_kept() -> None:
    lr = _REG.type("lab_result")
    # Lifecycle facet removed -> the functional one-per-entity `status` is gone.
    assert "Lifecycle" not in lr.facets
    assert "status" not in {p.canonical_name for p in lr.effective_predicates}
    eff = _eff("lab_result")
    # `category` is kept as-is (the product.category collision is fictional, §12.2).
    assert eff["category"] == ("enum", "attribute", False)
    assert eff["value"] == ("quantity", "measurement", False)
    assert eff["component"] == ("quantity", "measurement", False)
    # identifier stays functional (matches the ExternalIdentified facet).
    assert eff["identifier"] == ("scalar", "attribute", True)


def test_lab_result_reading_vocabulary_present() -> None:
    eff = _eff("lab_result")
    for name in ("value", "referenceRange", "interpretation", "specimen",
                 "effectiveDate", "identifier", "category", "performer", "component"):
        assert name in eff, name


# --- new encounter type (§3.4) -------------------------------------------


def test_encounter_declares_collision_audited_vocabulary() -> None:
    own = {p.canonical_name for p in _REG.type("encounter").own_predicates}
    expected = {"period", "class", "careUnit", "serviceProvider", "attender",
                "encounterDiagnosis", "transfusion", "partOfEncounter",
                "disposition", "hasObservation"}
    assert expected <= own
    # It must NOT reuse the colliding shipped names.
    assert "location" not in own and "partOf" not in own and "reasonCode" not in own


def test_medical_condition_identifier_uses_id_scheme() -> None:
    p = _REG.predicate_for_kind("medical_condition", "identifier")
    assert p is not None
    assert (p.value_shape, p.kind, p.qualifier_vocab) == ("scalar", "attribute", "id_scheme")


# --- the global collision audit (§3.4) -----------------------------------


def test_category_seeds_as_its_own_enum_row_no_collapse() -> None:
    s = _SEEDS["category"]
    assert (s.value_shape, s.kind) == ("enum", "attribute")


def test_new_encounter_names_seed_as_own_rows() -> None:
    def shape(name: str) -> tuple[str, str]:
        return (_SEEDS[name].value_shape, _SEEDS[name].kind)

    assert shape("careUnit") == ("text", "attribute")
    assert shape("encounterDiagnosis") == ("ref", "relationship")
    assert shape("partOfEncounter") == ("ref", "relationship")
    assert shape("hasObservation") == ("ref", "relationship")


def test_avoided_names_keep_their_shipped_shapes() -> None:
    # The names encounter deliberately did NOT reuse must be untouched.
    assert (_SEEDS["location"].value_shape, _SEEDS["location"].kind) == ("ref", "relationship")
    assert (_SEEDS["partOf"].value_shape, _SEEDS["partOf"].kind) == ("ref", "relationship")
    assert (_SEEDS["reasonCode"].value_shape, _SEEDS["reasonCode"].kind) == ("text", "attribute")


def test_identifier_seeds_once_scalar_attribute_functional() -> None:
    s = _SEEDS["identifier"]
    assert (s.value_shape, s.kind, s.functional) == ("scalar", "attribute", True)


# --- value-shape validation on the new vocabulary ------------------------


def test_new_vocabulary_validates() -> None:
    pv = _REG.predicate_for_kind("lab_result", "value")
    ptx = _REG.predicate_for_kind("encounter", "transfusion")
    pid = _REG.predicate_for_kind("medical_condition", "identifier")
    assert pv is not None and ptx is not None and pid is not None
    assert _REG.validate_value(pv, {"value": 9, "unit": "10*3/uL"}, object_present=False)
    tx = {"product": "FFP", "units": 2, "indication": "coag"}
    assert _REG.validate_value(ptx, tx, object_present=False)
    # icd10 validates with no _meta edit (open-scheme invariant, §3.8).
    assert _REG.validate_value(pid, {"value": "D69.6"}, object_present=False)
