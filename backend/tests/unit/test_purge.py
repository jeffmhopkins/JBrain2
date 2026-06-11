"""Chain-repair edge cases for the note-deletion purge (analysis/purge.py).

chain_repair_target is the pure core: given the doomed facts' own
superseded_by links, where (if anywhere) should a survivor pointing into the
doomed set re-attach?
"""

import uuid

from jbrain.analysis.purge import chain_repair_target


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
