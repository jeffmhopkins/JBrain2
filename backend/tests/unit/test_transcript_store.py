"""The carry-forward window (transcript_store._carry_forward_turn_ids): which earlier user
turns' images a vision-capable follow-up re-sees. The DB-backed recent_image_turns is
covered against real Postgres in tests/integration/test_turn_attachments_rls.py."""

from datetime import UTC, datetime, timedelta

from jbrain.agent.transcript_store import _carry_forward_turn_ids

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _rows(*ages_min: float) -> list[tuple[str, datetime]]:
    """User turns NEWEST-first, each labeled t0, t1, … and aged `ages_min` minutes before now.
    The defaults (last 4 turns / 15 min) match production, so the tests read the real window."""
    return [(f"t{i}", _NOW - timedelta(minutes=age)) for i, age in enumerate(ages_min)]


def test_union_of_last_four_turns_and_the_time_window() -> None:
    # 6 turns 5 min apart: last-4 is t0..t3; within 15 min is t0(0),t1(5),t2(10),t3(15). The
    # union is t0..t3, returned oldest-first (t3 is the oldest of the four).
    ids = _carry_forward_turn_ids(_rows(0, 5, 10, 15, 20, 25), now=_NOW)
    assert ids == ["t3", "t2", "t1", "t0"]


def test_time_window_extends_past_the_turn_count() -> None:
    # 8 turns 1 min apart: all within 15 min, so ALL eight carry forward even though that's
    # more than the 4-turn floor — "all within 15 (even if over 4)".
    ids = _carry_forward_turn_ids(_rows(0, 1, 2, 3, 4, 5, 6, 7), now=_NOW)
    assert ids == [f"t{i}" for i in range(7, -1, -1)]


def test_last_four_turns_carry_even_when_stale() -> None:
    # 6 turns an hour apart: only t0 is within 15 min, but the 4-turn floor keeps t0..t3 (the
    # "minimum last 4"); t4/t5 are both too old AND past the floor, so they drop.
    ids = _carry_forward_turn_ids(_rows(0, 60, 120, 180, 240, 300), now=_NOW)
    assert ids == ["t3", "t2", "t1", "t0"]


def test_boundary_is_inclusive_at_exactly_the_window() -> None:
    # A turn exactly at the 15-min edge is IN (>= cutoff); one a second past is OUT (and past
    # the 4-turn floor, so the floor doesn't rescue it either).
    edge = _carry_forward_turn_ids(_rows(0, 1, 2, 3, 4, 15), now=_NOW)
    assert "t5" in edge  # t5 at exactly 15 min is included
    past = _carry_forward_turn_ids(_rows(0, 1, 2, 3, 4, 15 + 1 / 60), now=_NOW)
    assert "t5" not in past  # a second past the window, and beyond the last 4, drops


def test_empty_history_carries_nothing() -> None:
    assert _carry_forward_turn_ids([], now=_NOW) == []
