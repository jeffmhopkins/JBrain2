"""Unit tests for the pure location-access pieces: the full-owner gate and the
enter→exit dwell pairing (no DB)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from jbrain.db.session import SessionContext
from jbrain.locations import Dwell, LocationToolRefusal, require_full_owner
from jbrain.locations import _pair_dwells as pair_dwells

_BASE = datetime(2026, 6, 4, 12, 0, tzinfo=UTC)


def _full_owner() -> SessionContext:
    return SessionContext(principal_id="o", principal_kind="owner")


def _narrowed_owner() -> SessionContext:
    return SessionContext(
        principal_id="o", principal_kind="owner", domain_scopes=("location",), owner_scoped=True
    )


def _device() -> SessionContext:
    return SessionContext(principal_id="d", principal_kind="device_key", subject_id="s")


def test_require_full_owner_passes_for_full_owner() -> None:
    require_full_owner(_full_owner())  # no raise


@pytest.mark.parametrize("ctx", [_narrowed_owner(), _device(), SessionContext()])
def test_require_full_owner_refuses_non_full_owner(ctx: SessionContext) -> None:
    with pytest.raises(LocationToolRefusal):
        require_full_owner(ctx)


def test_refusal_message_is_owner_only() -> None:
    with pytest.raises(LocationToolRefusal, match="owner-only"):
        require_full_owner(_narrowed_owner())


@dataclass
class _Row:
    occurred_at: datetime
    transition: str
    eid: str | None
    place_name: str


def _row(minute: int, transition: str, eid: str | None = "P1", name: str = "Office") -> _Row:
    return _Row(_BASE + timedelta(minutes=minute), transition, eid, name)


def test_simple_enter_exit_pairs() -> None:
    until = _BASE + timedelta(hours=2)
    dwells = pair_dwells([_row(0, "enter"), _row(30, "exit")], until=until)
    assert dwells == [
        Dwell("P1", "Office", _BASE, _BASE + timedelta(minutes=30), 30 * 60.0),
    ]


def test_open_enter_clamps_to_until() -> None:
    until = _BASE + timedelta(minutes=45)
    dwells = pair_dwells([_row(0, "enter")], until=until)
    assert len(dwells) == 1
    assert dwells[0].exited_at == until and dwells[0].seconds == 45 * 60.0


def test_orphan_exit_dropped() -> None:
    until = _BASE + timedelta(hours=2)
    assert pair_dwells([_row(10, "exit")], until=until) == []


def test_non_positive_interval_dropped() -> None:
    until = _BASE + timedelta(hours=2)
    # exit at the same instant as enter: zero-length, no real stay.
    assert pair_dwells([_row(5, "enter"), _row(5, "exit")], until=until) == []


def test_two_places_paired_independently() -> None:
    until = _BASE + timedelta(hours=3)
    rows = [
        _row(0, "enter", "A", "Home"),
        _row(10, "enter", "B", "Gym"),
        _row(20, "exit", "A", "Home"),
        _row(40, "exit", "B", "Gym"),
    ]
    dwells = pair_dwells(rows, until=until)
    by_place = {d.place_entity_id: d for d in dwells}
    assert by_place["A"].seconds == 20 * 60.0
    assert by_place["B"].seconds == 30 * 60.0


def test_duplicate_enter_does_not_shorten_stay() -> None:
    until = _BASE + timedelta(hours=2)
    rows = [_row(0, "enter"), _row(10, "enter"), _row(50, "exit")]
    dwells = pair_dwells(rows, until=until)
    assert len(dwells) == 1
    assert dwells[0].entered_at == _BASE and dwells[0].seconds == 50 * 60.0


def test_dwells_sorted_by_entry() -> None:
    until = _BASE + timedelta(hours=3)
    rows = [
        _row(60, "enter", "B", "Gym"),
        _row(0, "enter", "A", "Home"),
        _row(90, "exit", "B", "Gym"),
        _row(30, "exit", "A", "Home"),
    ]
    dwells = pair_dwells(rows, until=until)
    assert [d.place_entity_id for d in dwells] == ["A", "B"]


def test_null_place_id_row_skipped() -> None:
    until = _BASE + timedelta(hours=2)
    rows = [_row(0, "enter", eid=None), _row(30, "exit", eid=None)]
    assert pair_dwells(rows, until=until) == []
