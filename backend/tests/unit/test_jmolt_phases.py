"""Restraint as structure (JMOLT_LEDGER_ENGINE_PLAN.md, S2).

The claim being tested is narrow and mechanical: for most of the night the publishing tools are
not in the schema. Not discouraged — absent. Every "post sparingly" line is a soft constraint
on a 120B, and the night that produced seventeen comments on one post ran under a prompt that
asked for restraint.
"""

from itertools import groupby

import pytest

from jbrain.agent.jmolt_phases import PUBLISHING_TOOLS, hidden_for, phase_for


def _shape(budget: int) -> list[str]:
    return [phase_for(i, budget=budget) for i in range(1, budget + 1)]


@pytest.mark.parametrize("budget", [3, 4, 6, 8, 10, 12, 20])
def test_most_of_the_night_cannot_publish(budget: int) -> None:
    """ "Most of the hour" is meant literally, at every budget a night might get."""
    shape = _shape(budget)
    assert shape.count("writing") < len(shape) / 2


@pytest.mark.parametrize("budget", [2, 3, 6, 12])
def test_the_night_always_ends_by_tending(budget: int) -> None:
    """An agent that never closes an obligation accumulates a brief it cannot finish reading,
    and that pressure is the same shape as never having posted."""
    assert _shape(budget)[-1] == "tending"


@pytest.mark.parametrize("budget", [3, 6, 12])
def test_the_night_always_begins_by_reading(budget: int) -> None:
    """The phase the current engine effectively never has: its first sitting can post, so its
    first sitting does."""
    assert _shape(budget)[0] == "reading"


@pytest.mark.parametrize("budget", [3, 4, 6, 8, 12])
def test_writing_is_one_window_not_a_permission_that_stays_open(budget: int) -> None:
    """So "I have not posted yet" cannot become pressure that builds all night."""
    shape = _shape(budget)
    runs = [k for k, _ in groupby(shape)]
    assert runs == ["reading", "writing", "tending"]


@pytest.mark.parametrize("budget", [3, 4, 6, 8, 12, 20])
def test_a_night_that_could_never_publish_is_a_different_experiment(budget: int) -> None:
    assert "writing" in _shape(budget)


def test_a_single_sitting_night_reads_rather_than_posting_on_an_unchecked_impulse() -> None:
    assert _shape(1) == ["reading"]


def test_a_two_sitting_night_gives_up_writing_before_it_gives_up_tending() -> None:
    """A short night loses writing time rather than the sitting where it tidies up."""
    assert _shape(2) == ["reading", "tending"]


def test_only_the_writing_window_has_the_publishing_tools() -> None:
    assert hidden_for("writing") == frozenset()
    assert hidden_for("reading") == PUBLISHING_TOOLS
    assert hidden_for("tending") == PUBLISHING_TOOLS


def test_hiding_fails_closed() -> None:
    """The failure that costs something here is the one that hands the agent a tool it was
    meant to be without."""
    assert hidden_for("something-nobody-defined") == PUBLISHING_TOOLS  # type: ignore[arg-type]


def test_voting_and_following_are_never_taken_away() -> None:
    """A vote is not a claim, and following someone is how an interest becomes durable.
    Throttling those would suppress the behaviour the engine is trying to grow, not the one it
    is trying to stop."""
    for phase in ("reading", "writing", "tending"):
        hidden = hidden_for(phase)  # type: ignore[arg-type]
        assert "moltbook_vote" not in hidden
        assert "moltbook_social" not in hidden
