"""`predicate_review` seam logic (no DB): the suggested-resolution picker (map onto a strong
neighbor, else mint) and the kill-switch gate refusing before any staging. Candidate selection +
staging are integration-tested (they need Postgres)."""

from typing import Any

import pytest

from jbrain.agent.predicatereview import PredicateReviewAction, _proposed_resolution
from jbrain.queue import PermanentJobError


def test_proposed_resolution_maps_onto_a_strong_neighbor() -> None:
    action, canonical = _proposed_resolution({"suggestions": [["spouse", 0.71], ["knows", 0.4]]})
    assert action == "map_to_existing" and canonical == "spouse"


def test_proposed_resolution_mints_when_no_neighbor_clears_the_band() -> None:
    action, canonical = _proposed_resolution({"suggestions": [["color", 0.30]]})
    assert action == "accept_as_new" and canonical is None


def test_proposed_resolution_mints_with_no_suggestions() -> None:
    action, canonical = _proposed_resolution({"suggestions": []})
    assert action == "accept_as_new" and canonical is None


class _FakeSettings:
    def __init__(self, *, kill: bool) -> None:
        self._kill = kill

    async def self_improvement_kill_switch(self, ctx: Any) -> bool:
        return self._kill

    async def self_improvement_daily_budget(self, ctx: Any) -> int:
        return 200_000

    async def self_improvement_spent_today(self, ctx: Any, *, day: str) -> int:
        return 0


class _FakeProposals:
    def __init__(self) -> None:
        self.staged: list[Any] = []

    async def stage(self, ctx: Any, *, principal_id: str, spec: Any) -> str:
        self.staged.append(spec)
        return "prop-1"


async def test_run_refused_when_kill_switch_on() -> None:
    proposals = _FakeProposals()
    action = PredicateReviewAction(
        None,  # type: ignore[arg-type]  # maker untouched — the gate refuses first
        settings=_FakeSettings(kill=True),  # type: ignore[arg-type]
        proposals=proposals,  # type: ignore[arg-type]
    )
    with pytest.raises(PermanentJobError):
        await action.run({})
    assert proposals.staged == []  # nothing staged behind the gate
