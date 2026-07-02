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


def test_shipped_registry_loads_all_catalog_types(registry: SchemaRegistry) -> None:
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
        # Productivity / knowledge / lifestyle types.
        "project",
        "task",
        "goal",
        "trip",
        "creative_work",
        "product",
        "habit",
        "insurance_policy",
    }
    assert set(registry.types) == expected
    assert registry.meta.schema_version >= 1


def test_person_residence_is_homelocation_not_generic_location(registry: SchemaRegistry) -> None:
    # A person's home is the functional homeLocation (a Place ref); the drift
    # spellings fold to it, but the generic `location` does NOT (it stays canonical
    # for event/org venues — global rename would mis-fold it). `residence` is ALSO
    # left alone: it is allowlist-functional with its own existing history, so a
    # global rename would silently rewrite it onto homeLocation (cf. worksFor/employer).
    home = registry.predicate_for_kind("Person", "homeLocation")
    assert home is not None and home.value_shape == "ref" and home.range_type == "place"
    assert home.functional  # one current home; former residences are closed history
    assert registry.normalize_predicate("livesIn") == "homeLocation"
    assert registry.normalize_predicate("residence") == "residence"  # historied; not folded
    assert registry.normalize_predicate("location") == "location"  # untouched


def test_person_long_tail_predicates_are_tier_2(registry: SchemaRegistry) -> None:
    # The entity-graph refocus demoted Person's long-tail predicates: undeclared
    # (tier-2) means stored raw with no registry treatment — never a rejection.
    for demoted in ("weight", "height", "goal", "siblingCount", "knowsLanguage", "nationality"):
        assert registry.predicate_for_kind("Person", demoted) is None
        assert not registry.declares_predicate(demoted)
    # Demotion lapses the renamed_from attractors too: drift spellings pass through.
    assert registry.normalize_predicate("speaksLanguage") == "speaksLanguage"
    assert registry.normalize_predicate("bodyHeight") == "bodyHeight"
    # The tier-1 survivors next to them keep full treatment.
    birth = registry.predicate_for_kind("Person", "birthPlace")
    assert birth is not None and birth.value_shape == "ref" and birth.range_type == "place"
    assert registry.normalize_predicate("bornIn") == "birthPlace"
    assert registry.normalize_predicate("goal") == "goal"  # generic; not a rename target


def test_person_carries_family_kinship_edges(registry: SchemaRegistry) -> None:
    # Family edges are accumulating (a person has many relatives), so none are
    # functional, and the common drift spellings fold to the schema.org canonical.
    for canonical in ("parent", "children", "sibling", "relative"):
        edge = registry.predicate_for_kind("Person", canonical)
        assert edge is not None and edge.value_shape == "ref"
        assert edge.range_type == "person"
        assert not edge.functional  # accumulating: a person has many relatives
    assert registry.normalize_predicate("mother") == "parent"
    assert registry.normalize_predicate("son") == "children"  # schema.org plural canonical
    assert registry.normalize_predicate("brother") == "sibling"
    # Social / work / ownership synonyms collapse to one canonical edge (the
    # persona-run predicate sprawl: buddy/coworker/possesses minted five ways).
    assert registry.normalize_predicate("buddy") == "friend"
    assert registry.normalize_predicate("worksWith") == "colleague"
    assert registry.normalize_predicate("coworker") == "colleague"
    assert registry.normalize_predicate("possesses") == "owns"


def test_person_treatedby_is_a_declared_care_edge(registry: SchemaRegistry) -> None:
    # Promoted by the entity-graph refocus: the prompt-MANDATED patient->provider
    # edge was registry-unknown, so every treatedBy fact ate the unknown-predicate
    # treatment. Accumulating (many providers), so NOT functional; drift spellings
    # fold; the inverse hasTreated stays reciprocity-map-only (not declared).
    treated = registry.predicate_for_kind("Person", "treatedBy")
    assert treated is not None and treated.value_shape == "ref"
    assert treated.range_type == "person" and treated.kind == "relationship"
    assert not treated.functional
    for drift in ("treated_by", "seenBy", "caredForBy"):
        assert registry.normalize_predicate(drift) == "treatedBy"
    assert not registry.declares_predicate("hasTreated")


def test_priority_is_a_shared_facet_so_members_agree(registry: SchemaRegistry) -> None:
    # priority lives in the Prioritized facet, so every consumer (goal/project/task)
    # shares one definition and enum_values_for never collapses to () on drift.
    assert set(registry.enum_values_for("priority")) == {"low", "medium", "high"}
    for tid in ("goal", "project", "task"):
        pri = registry.type(tid).predicate("priority")
        assert pri is not None and pri.value_shape == "enum" and pri.functional


def test_insurance_policy_is_the_insurer_endpoint(registry: SchemaRegistry) -> None:
    # The policy object the product/vehicle/device "insurer" Role edges point at.
    insurer = registry.predicate_for_kind("insurance_policy", "insurer")
    assert insurer is not None and insurer.range_type == "organization"
    assert registry.normalize_predicate("carrier") == "insurer"
    # `provider` (subscription's canonical) is NOT hijacked by the carrier attractor.
    assert registry.normalize_predicate("provider") == "provider"
    insures = registry.predicate_for_kind("insurance_policy", "insures")
    assert insures is not None and insures.range_type is None  # polymorphic


def test_creative_work_does_not_hijack_generic_creator(registry: SchemaRegistry) -> None:
    # `creator` is too generic to globally rewrite onto creative_work.author.
    assert registry.normalize_predicate("creator") == "creator"
    assert registry.normalize_predicate("goal") == "goal"  # project.contributesTo drops it too


def test_named_types_have_a_real_display_name_target(registry: SchemaRegistry) -> None:
    # medication/lab_result project their display name from name.* (the Named facet
    # was missing before); every display_name token must resolve to a predicate.
    for tid in ("medication", "lab_result"):
        t = registry.type(tid)
        names = {p.canonical_name for p in t.effective_predicates}
        assert "name" in names
        for token in t.display_name:
            for part in token.split("+"):
                assert part in names


def test_person_no_longer_declares_pronouns(registry: SchemaRegistry) -> None:
    # pronouns was dropped from the Person catalog; it is not a declared predicate
    # (it remains storable as any open predicate — the registry never gates names).
    assert registry.predicate_for_kind("Person", "pronouns") is None
    assert not registry.declares_predicate("pronouns")


def test_project_task_goal_form_a_rollup_graph(registry: SchemaRegistry) -> None:
    # A task belongs to a project (partOf), a project serves a goal (contributesTo),
    # and a task may also serve a goal directly. All are typed ref edges, and the
    # project/goal hierarchy refs are self-referential.
    part_of = registry.predicate_for_kind("task", "partOf")
    assert part_of is not None and part_of.value_shape == "ref"
    assert part_of.range_type == "project" and part_of.functional
    contributes = registry.predicate_for_kind("project", "contributesTo")
    assert contributes is not None and contributes.range_type == "goal"
    parent_goal = registry.predicate_for_kind("goal", "parentGoal")
    assert parent_goal is not None and parent_goal.range_type == "goal"
    blocked_by = registry.predicate_for_kind("task", "blockedBy")
    assert blocked_by is not None and blocked_by.range_type == "task"  # self-ref


def test_task_due_date_drift_and_functionality(registry: SchemaRegistry) -> None:
    # dueDate is the shared canonical (also used by bill); it is functional on a
    # task (a reschedule supersedes) and `dueBy` drifts onto it.
    assert registry.normalize_predicate("dueBy") == "dueDate"
    assert registry.is_functional("dueDate")
    # `deadline` is the soft attractor for the project/goal targetDate horizon.
    assert registry.normalize_predicate("deadline") == "targetDate"


def test_facets_roll_down_into_types(registry: SchemaRegistry) -> None:
    person = registry.type("person")
    names = {p.canonical_name for p in person.effective_predicates}
    # From the Named facet…
    assert {"name", "name.full", "name.nickname", "name.given"} <= names
    # …alongside the type's own predicates.
    assert "birthDate" in names and "spouse" in names


def test_person_worksfor_is_a_declared_functional_org_edge(registry: SchemaRegistry) -> None:
    # worksFor is the live pipeline's employer edge (extract/integrate prompts,
    # rel_employer_change). Declaring it kills the recurring `new_predicate` card
    # and the unknown-predicate weight penalty that held legit past employers in
    # review. Drift spellings normalize to it, and it is functional (one current
    # employer; former jobs are closed history).
    works = registry.predicate_for_kind("Person", "worksFor")
    assert works is not None
    assert works.value_shape == "ref"
    assert works.range_type == "organization"
    assert registry.declares_predicate("worksFor")
    assert registry.declares_predicate("works_for")  # renamed_from drift spelling -> worksFor
    assert registry.is_functional("worksFor")
    assert registry.is_functional("works_for")
    # The folded-qualifier split stays a no-op (worksFor takes no qualifier vocab).
    assert registry.decompose_predicate("worksFor.contractor", "") == ("worksFor.contractor", "")


def test_lifecycle_status_enum_is_filled_from_status_values(registry: SchemaRegistry) -> None:
    status = registry.type("appointment").predicate("status")
    assert status is not None
    assert status.value_shape == "enum"
    assert set(status.enum_values) == {"tentative", "confirmed", "cancelled", "occurred"}


def test_person_gender_is_a_closed_enum(registry: SchemaRegistry) -> None:
    # gender is a closed set, not free text: a member commits, a non-member is a
    # shape violation routed to review (the storage invariant still keeps the
    # fact on its statement — validate_value only gates value_json).
    gender = registry.predicate_for_kind("Person", "gender")
    assert gender is not None
    assert gender.value_shape == "enum"
    assert set(gender.enum_values) == {"male", "female", "unknown"}
    assert registry.validate_value(gender, {"value": "Female"}, object_present=False)
    assert not registry.validate_value(gender, {"value": "wife"}, object_present=False)


def test_enum_values_for_returns_members_kind_agnostically(registry: SchemaRegistry) -> None:
    # The review card's correct-in-place picker reads members without an entity
    # kind: gender → its closed set, a drift spelling normalizes first, and a
    # free-text or unknown predicate yields () (no picker, free-text edit).
    assert set(registry.enum_values_for("gender")) == {"male", "female", "unknown"}
    assert registry.enum_values_for("name.full") == ()
    assert registry.enum_values_for("totallyMadeUpPredicate") == ()


def test_coerce_value_normalizes_enum_prose(registry: SchemaRegistry) -> None:
    # The screenshot bug: the model wrote its rationale into the value. Coercion
    # pulls the bare member out so the card reads "female", not the prose — and
    # "male" inside "female" never mis-fires (whole-word match, exactly one).
    gender = registry.predicate_for_kind("Person", "gender")
    assert gender is not None
    assert registry.coerce_value(gender, {"value": "Female (inferred from 'wife')."}) == {
        "value": "female"
    }
    assert registry.coerce_value(gender, {"value": "Male"}) == {"value": "male"}
    # Already canonical, ambiguous, or no member: left untouched (validate_value gates).
    assert registry.coerce_value(gender, {"value": "female"}) == {"value": "female"}
    assert registry.coerce_value(gender, {"value": "either male or female"}) == {
        "value": "either male or female"
    }
    assert registry.coerce_value(gender, {"value": "nonbinary"}) == {"value": "nonbinary"}
    # A non-enum predicate is never touched.
    full = registry.predicate_for_kind("Person", "name.full")
    assert full is not None
    assert registry.coerce_value(full, {"value": "Celine"}) == {"value": "Celine"}


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


def test_decompose_recovers_a_qualifier_folded_into_the_predicate(
    registry: SchemaRegistry,
) -> None:
    # The screenshot bug: a model folds the audience into the dotted path. The
    # base (name.nickname) takes a qualifier_vocab, so the trailing segment splits
    # back out into the qualifier instead of minting a spurious new predicate.
    assert registry.decompose_predicate("name.nickname.kids", "") == ("name.nickname", "kids")
    # Drift base + fold compose: nickname -> name.nickname, then .work splits off.
    assert registry.decompose_predicate("nickname.work", "") == ("name.nickname", "work")
    # identifier takes a scheme qualifier the same way.
    assert registry.decompose_predicate("identifier.vin", "") == ("identifier", "vin")


def test_decompose_leaves_well_formed_and_novel_predicates_untouched(
    registry: SchemaRegistry,
) -> None:
    # Already correct (declared predicate + explicit qualifier): no split.
    assert registry.decompose_predicate("name.nickname", "kids") == ("name.nickname", "kids")
    # A dotted CANONICAL is declared, so it is never torn apart.
    assert registry.decompose_predicate("name.full", "") == ("name.full", "")
    # A genuine novel a.b whose base takes no qualifier stays whole (-> commits
    # raw as tier-2 long-tail, unchanged).
    assert registry.decompose_predicate("worksFor.contractor", "") == ("worksFor.contractor", "")
    # Never overwrite an explicit qualifier, even on a folded-looking predicate.
    assert registry.decompose_predicate("name.nickname.kids", "work") == (
        "name.nickname.kids",
        "work",
    )


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


def test_display_name_token_must_be_a_property(tmp_path: Path) -> None:
    # A display_name token (or a `+` composite part) that is not a declared
    # predicate would silently break the canonical-name projection — fail at load.
    _write_min_registry(tmp_path)
    (tmp_path / "types" / "person.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "person",
                "name": "Person",
                "facets": ["Named"],
                "display_name": ["name+ghostField"],
            }
        )
    )
    with pytest.raises(SchemaError, match="display_name token"):
        load_registry(tmp_path)
