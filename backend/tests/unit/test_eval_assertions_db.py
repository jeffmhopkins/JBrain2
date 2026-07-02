"""Unit tests for the DB-mode eval gate (tests/eval/assertions.check_case_db).

Pure, CI-runnable: builds DbCommit fixtures by hand (no Postgres, no Grok) and
proves the gate catches each committed-state bug class — wrong disposition,
review fact without a card, an un-closed supersession, a forked duplicate, a
domain-floor leak — and passes a clean commit. A green real-Grok DB run only
means something if this logic is itself proven.
"""

from typing import Any

from tests.eval.assertions import check_case_db
from tests.eval.cases import (
    CommittedFact,
    DbCommit,
    ReviewCard,
    SeededFactState,
    case_from_dict,
)

OWNER = "owner-uuid"


def _cf(entity_id: str, entity_name: str, predicate: str, **kw: Any) -> CommittedFact:
    base: dict[str, Any] = dict(
        id="f1",
        qualifier="",
        kind="attribute",
        value_json=None,
        assertion="asserted",
        status="active",
        domain_code="general",
        object_entity_id=None,
        object_name=None,
    )
    base.update(kw)
    return CommittedFact(entity_id=entity_id, entity_name=entity_name, predicate=predicate, **base)


def _commit(
    facts: tuple[CommittedFact, ...],
    *,
    entities: dict[str, str] | None = None,
    seeded_ids: dict[str, str] | None = None,
    review_fact_ids: frozenset[str] = frozenset(),
    seeded_facts: tuple[SeededFactState, ...] = (),
    review_cards: tuple[ReviewCard, ...] = (),
) -> DbCommit:
    return DbCommit(
        owner_id=OWNER,
        note_id="n1",
        seeded_ids=seeded_ids or {},
        facts=facts,
        entities=entities if entities is not None else {OWNER: "Me"},
        review_fact_ids=review_fact_ids,
        seeded_facts=seeded_facts,
        review_cards=review_cards,
    )


# --- former (closed interval) assertion -------------------------------------


def test_former_assertion_passes_for_a_closed_edge() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "used to work for X",
            "expect": {
                "facts": [{"entity": "Me", "predicate": "worksFor", "object": "X", "former": True}]
            },
        }
    )
    commit = _commit(
        (_cf(OWNER, "Me", "worksFor", kind="relationship", object_name="X", valid_to="2026-06-15"),)
    )
    assert check_case_db(case, commit) == []


def test_former_assertion_catches_an_open_edge_expected_closed() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "used to work for X",
            "expect": {
                "facts": [{"entity": "Me", "predicate": "worksFor", "object": "X", "former": True}]
            },
        }
    )
    commit = _commit(  # valid_to None → reads as current, but the case wanted former
        (_cf(OWNER, "Me", "worksFor", kind="relationship", object_name="X", valid_to=None),)
    )
    fails = check_case_db(case, commit)
    assert any("former" in f for f in fails)


def test_former_false_requires_an_open_current_edge() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "works for X",
            "expect": {
                "facts": [{"entity": "Me", "predicate": "worksFor", "object": "X", "former": False}]
            },
        }
    )
    commit = _commit(  # closed, but the case expected the current (open) value
        (_cf(OWNER, "Me", "worksFor", kind="relationship", object_name="X", valid_to="2026-06-15"),)
    )
    assert any("former" in f for f in check_case_db(case, commit))


# --- dispositions: active commit vs pending_review + card --------------------


def test_clean_active_commit_passes() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"facts": [{"entity": "Me", "predicate": "weight", "disposition": "commit"}]},
        }
    )
    commit = _commit((_cf(OWNER, "Me", "weight", id="f1", status="active"),))
    assert check_case_db(case, commit) == []


def test_should_commit_but_held_for_review_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"facts": [{"entity": "Me", "predicate": "weight", "disposition": "commit"}]},
        }
    )
    # Status pending_review + a card → wrong: the case expected a clean commit.
    commit = _commit(
        (_cf(OWNER, "Me", "weight", id="f1", status="pending_review"),),
        review_fact_ids=frozenset({"f1"}),
    )
    fails = check_case_db(case, commit)
    assert any("expected active commit" in f for f in fails)


def test_review_fact_with_card_passes() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {
                "facts": [{"entity": "Mara", "predicate": "birthDate", "disposition": "review"}]
            },
        }
    )
    commit = _commit(
        (_cf("e-mara", "Mara", "birthDate", id="f1", status="pending_review"),),
        entities={OWNER: "Me", "e-mara": "Mara"},
        review_fact_ids=frozenset({"f1"}),
    )
    assert check_case_db(case, commit) == []


def test_review_fact_without_card_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {
                "facts": [{"entity": "Mara", "predicate": "birthDate", "disposition": "review"}]
            },
        }
    )
    # pending_review row but NO linking card → the review path didn't file a card.
    commit = _commit(
        (_cf("e-mara", "Mara", "birthDate", id="f1", status="pending_review"),),
        entities={OWNER: "Me", "e-mara": "Mara"},
    )
    fails = check_case_db(case, commit)
    assert any("expected pending_review + card" in f for f in fails)


# --- value fidelity on the committed row -------------------------------------


def test_committed_value_json_sentence_regression_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {
                "facts": [{"entity": "Me", "predicate": "bloodType", "value": "O negative"}]
            },
        }
    )
    commit = _commit((_cf(OWNER, "Me", "bloodType", value_json=None),))
    fails = check_case_db(case, commit)
    assert any("value_json is None" in f for f in fails)


# --- supersession EFFECT ------------------------------------------------------


def test_supersession_closure_passes() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"supersede": [{"entity": "Me", "predicate": "worksFor"}]},
        }
    )
    commit = _commit(
        (),
        seeded_facts=(
            SeededFactState(
                entity_symbolic="owner-1",
                entity_name="Me",
                predicate="worksFor",
                status="superseded",
                superseded_by="new-fact",
                valid_to="2026-06-14",
            ),
        ),
    )
    assert check_case_db(case, commit) == []


def test_supersession_not_closed_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"supersede": [{"entity": "Me", "predicate": "worksFor"}]},
        }
    )
    # Prior edge still active → supersession never happened.
    commit = _commit(
        (),
        seeded_facts=(
            SeededFactState(
                entity_symbolic="owner-1",
                entity_name="Me",
                predicate="worksFor",
                status="active",
                superseded_by=None,
                valid_to=None,
            ),
        ),
    )
    fails = check_case_db(case, commit)
    assert any("not superseded" in f for f in fails)


# --- resolve-to-existing: no forked duplicate --------------------------------


def test_resolve_to_existing_passes() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {
                "resolutions": [{"mention": "Theo", "mode": "existing", "entity_id": "ent-theo"}]
            },
        }
    )
    # The note's fact landed on the seeded entity; no new "Theo" row.
    commit = _commit(
        (_cf("theo-uuid", "Theo Hopkins", "scored"),),
        entities={OWNER: "Me", "theo-uuid": "Theo Hopkins"},
        seeded_ids={"ent-theo": "theo-uuid"},
    )
    assert check_case_db(case, commit) == []


def test_resolve_to_existing_forked_duplicate_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {
                "resolutions": [{"mention": "Theo", "mode": "existing", "entity_id": "ent-theo"}]
            },
        }
    )
    # A brand-new "Theo" entity (not the seeded UUID) → forked a duplicate.
    commit = _commit(
        (_cf("new-theo", "Theo", "scored"),),
        entities={OWNER: "Me", "theo-uuid": "Theo Hopkins", "new-theo": "Theo"},
        seeded_ids={"ent-theo": "theo-uuid"},
    )
    fails = check_case_db(case, commit)
    assert any("forked a new entity" in f for f in fails)


# --- domain firewall floor on the committed row ------------------------------


def test_domain_floor_passes() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "domain": "general",
            "expect": {"facts": [{"entity": "Mom", "predicate": "medication", "domain": "health"}]},
        }
    )
    commit = _commit(
        (_cf("e-mom", "Mom", "medication", domain_code="health"),),
        entities={OWNER: "Me", "e-mom": "Mom"},
    )
    assert check_case_db(case, commit) == []


def test_domain_floor_leak_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"facts": [{"entity": "Mom", "predicate": "medication", "domain": "health"}]},
        }
    )
    # Health predicate committed under general → firewall floor leaked.
    commit = _commit(
        (_cf("e-mom", "Mom", "medication", domain_code="general"),),
        entities={OWNER: "Me", "e-mom": "Mom"},
    )
    fails = check_case_db(case, commit)
    assert any("domain 'general' != 'health'" in f for f in fails)


# --- forbidden entity / max_entities / max_facts -----------------------------


def test_forbidden_entity_committed_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"forbidden_entities": ["EvilCorp"]},
        }
    )
    commit = _commit(
        (_cf("evil", "EvilCorp", "worksFor"),),
        entities={OWNER: "Me", "evil": "EvilCorp"},
    )
    fails = check_case_db(case, commit)
    assert any("forbidden entity committed" in f for f in fails)


def test_max_entities_exceeded_is_caught() -> None:
    case = case_from_dict({"id": "c", "note_text": "x", "expect": {"max_entities": 1}})
    commit = _commit(
        (),
        entities={OWNER: "Me", "a": "Mom", "b": "CeeCee"},
    )
    fails = check_case_db(case, commit)
    assert any("too many entities" in f for f in fails)


def test_domain_floor_count_passes() -> None:
    case = case_from_dict(
        {"id": "c", "note_text": "x", "expect": {"committed_domains": {"health": 1}}}
    )
    commit = _commit((_cf("e", "Mom", "medication", domain_code="health"),))
    assert check_case_db(case, commit) == []


def test_domain_floor_count_missing_is_caught() -> None:
    case = case_from_dict(
        {"id": "c", "note_text": "x", "expect": {"committed_domains": {"health": 1}}}
    )
    # The note committed nothing under health → floor not met.
    commit = _commit((_cf("e", "Me", "errand", domain_code="general"),))
    fails = check_case_db(case, commit)
    assert any("domain floor" in f and "health" in f for f in fails)


def test_absent_fact_committed_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {
                "absent_facts": [{"entity": "Me", "predicate": "worksFor", "object": "EvilCorp"}]
            },
        }
    )
    commit = _commit((_cf(OWNER, "Me", "worksFor", object_name="EvilCorp"),))
    fails = check_case_db(case, commit)
    assert any("forbidden committed fact present" in f for f in fails)


# --- new_predicate review cards (Phase 4) ------------------------------------


def test_review_card_present_passes() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"review_cards": [{"kind": "new_predicate", "min_suggestions": 1}]},
        }
    )
    commit = _commit(
        (),
        review_cards=(
            ReviewCard(
                kind="new_predicate", predicate="favoriteColor", suggestions=(("hue", 0.8),)
            ),
        ),
    )
    assert check_case_db(case, commit) == []


def test_review_card_missing_is_caught() -> None:
    case = case_from_dict(
        {"id": "c", "note_text": "x", "expect": {"review_cards": [{"kind": "new_predicate"}]}}
    )
    fails = check_case_db(case, _commit(()))  # no cards filed
    assert any("expected review card" in f for f in fails)


def test_absent_review_card_clean_commit_passes() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"absent_review_cards": [{"kind": "new_predicate"}]},
        }
    )
    # Long-tail predicate committed raw; no card filed → the negative gate passes.
    commit = _commit((_cf(OWNER, "Me", "favoriteColor", value_json={"value": "teal"}),))
    assert check_case_db(case, commit) == []


def test_absent_review_card_filed_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"absent_review_cards": [{"kind": "new_predicate"}]},
        }
    )
    card = ReviewCard(kind="new_predicate", predicate="favoriteColor", suggestions=())
    fails = check_case_db(case, _commit((), review_cards=(card,)))
    assert any("forbidden review card filed" in f for f in fails)


def test_absent_review_card_predicate_scoped_ignores_other_predicates() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {
                "absent_review_cards": [{"kind": "new_predicate", "predicate": "favoriteColor"}]
            },
        }
    )
    # A card for a DIFFERENT predicate doesn't trip a predicate-scoped spec.
    commit = _commit(
        (), review_cards=(ReviewCard(kind="new_predicate", predicate="earWiggle", suggestions=()),)
    )
    assert check_case_db(case, commit) == []


def test_review_card_too_few_suggestions_is_caught() -> None:
    case = case_from_dict(
        {
            "id": "c",
            "note_text": "x",
            "expect": {"review_cards": [{"kind": "new_predicate", "min_suggestions": 1}]},
        }
    )
    # A cold card with no suggestions fails a WEAK (>=1 suggestion) expectation.
    commit = _commit(
        (), review_cards=(ReviewCard(kind="new_predicate", predicate="x", suggestions=()),)
    )
    fails = check_case_db(case, commit)
    assert any("expected review card" in f for f in fails)
