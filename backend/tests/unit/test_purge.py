"""Chain-repair edge cases for the note-deletion purge (analysis/purge.py).

chain_repair_target is the pure core: given the doomed facts' own
superseded_by links, where (if anywhere) should a survivor pointing into the
doomed set re-attach?
"""

import uuid

from jbrain.analysis.purge import (
    _EFFECT_FACT_KEYS,
    _EFFECT_MENTION_KEYS,
    _effect_named_ids_sql,
    chain_repair_target,
)


def ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_supersessor_not_doomed_is_returned_unchanged() -> None:
    # Defensive case: callers only pass survivors pointing INTO the doomed
    # set, but a non-doomed start must come straight back.
    survivor_target, doomed = ids(2)
    assert chain_repair_target(survivor_target, {doomed: None}) is survivor_target


def test_chain_dying_in_doomed_set_restores() -> None:
    (doomed,) = ids(1)
    assert chain_repair_target(doomed, {doomed: None}) is None


def test_multi_hop_through_doomed_links_reattaches_to_survivor() -> None:
    d1, d2, surviving = ids(3)
    assert chain_repair_target(d1, {d1: d2, d2: surviving}) is surviving


def test_multi_hop_chain_ending_at_doomed_head_restores() -> None:
    d1, d2 = ids(2)
    assert chain_repair_target(d1, {d1: d2, d2: None}) is None


def test_cycle_in_doomed_links_treated_as_chain_dead() -> None:
    # A superseded_by cycle is corrupt data; the walk must terminate and the
    # survivor is restored rather than re-attached into garbage.
    d1, d2 = ids(2)
    assert chain_repair_target(d1, {d1: d2, d2: d1}) is None


def test_none_start_is_none() -> None:
    (doomed,) = ids(1)
    assert chain_repair_target(None, {doomed: None}) is None


# --- the effects arm of the rebuild spare set -----------------------------


def test_effect_sql_covers_every_key_it_is_given() -> None:
    """The spare set and the wipe must read one list, so the SQL is GENERATED from the
    key constants rather than hand-written: a key added to either tuple has to appear in
    the query on the same edit, or the row it names is purged under a card that will
    replay it."""
    for keys in (_EFFECT_MENTION_KEYS, _EFFECT_FACT_KEYS):
        sql = _effect_named_ids_sql(keys)
        for key in keys:
            assert f"eff.effect->'{key}'" in sql
        assert sql.count("FROM app.review_items ri") == len(keys)
        # Only cards the purge does NOT retire hold a replayable decision.
        assert "ri.status NOT IN :purged" in sql


def test_effect_sql_never_reads_a_non_array_as_an_array() -> None:
    """`resolution` is free-form jsonb with no schema behind it: a NULL resolution, an
    `effects` that is not an array, or an effect whose key holds a scalar must yield no
    rows rather than erroring the whole purge. Both hops are jsonb_typeof-guarded."""
    sql = _effect_named_ids_sql(_EFFECT_FACT_KEYS)
    assert sql.count("jsonb_typeof(ri.resolution->'effects') = 'array'") == len(_EFFECT_FACT_KEYS)
    assert sql.count("jsonb_typeof(eff.effect->") == len(_EFFECT_FACT_KEYS)
