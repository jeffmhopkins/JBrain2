"""The pure trail shaper: gap-split into legs, downsample under a point budget,
and the render-only view payload. No DB, no clock — a pure function over a fix
list, so these assert the shape the `location_map` view consumes (and that a gap
is NEVER bridged)."""

from datetime import UTC, datetime, timedelta

from jbrain.locations import FixPoint
from jbrain.locations.trail import (
    DEFAULT_MAX_GAP_SECONDS,
    build_trail,
    trail_view_data,
)

_BASE = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)


def _fix(
    minute: float,
    *,
    lat: float = 40.0,
    lon: float = -105.0,
    battery: int | None = None,
    accuracy: float | None = None,
) -> FixPoint:
    return FixPoint(
        captured_at=_BASE + timedelta(minutes=minute),
        latitude=lat,
        longitude=lon,
        accuracy_m=accuracy,
        battery_pct=battery,
    )


def test_empty_fix_list_is_empty_trail() -> None:
    trail = build_trail([])
    assert trail.is_empty
    assert trail.legs == [] and trail.gaps == []
    data = trail_view_data(trail)
    assert data["legs"] == [] and data["gaps"] == [] and data["total_fixes"] == 0


def test_single_point_is_one_leg_no_gap() -> None:
    trail = build_trail([_fix(0)])
    assert not trail.is_empty
    assert len(trail.legs) == 1 and trail.gaps == []
    leg = trail.legs[0]
    assert leg.fix_count == 1
    assert len(leg.points) == 1
    assert leg.distance_m == 0.0


def test_continuous_fixes_are_one_leg() -> None:
    # Fixes 1 minute apart — well under the max gap — stay a single leg.
    fixes = [_fix(i) for i in range(10)]
    trail = build_trail(fixes)
    assert len(trail.legs) == 1
    assert trail.gaps == []
    assert trail.legs[0].fix_count == 10


def test_a_gap_splits_into_separate_legs_never_bridged() -> None:
    # Two clusters separated by a > max-gap hole → two legs + one gap. The legs'
    # points stay disjoint: the second leg never includes the first's tail (no
    # bridge across the gap).
    gap_min = DEFAULT_MAX_GAP_SECONDS / 60 + 10
    leg1 = [_fix(i, lat=40.0) for i in range(3)]
    leg2 = [_fix(gap_min + i, lat=41.0) for i in range(3)]  # a distinct cluster
    trail = build_trail(leg1 + leg2)
    assert len(trail.legs) == 2
    assert len(trail.gaps) == 1
    gap = trail.gaps[0]
    assert gap.after_leg == 0
    assert gap.seconds == (leg2[0].captured_at - leg1[-1].captured_at).total_seconds()
    # The two legs hold disjoint clusters — leg 2 never includes leg 1's tail (the
    # gap is not bridged into one polyline).
    assert trail.legs[0].fix_count == 3 and trail.legs[1].fix_count == 3
    assert trail.legs[0].points[-1][0] == 40.0
    assert trail.legs[1].points[0][0] == 41.0


def test_downsample_bounds_total_points_keeping_endpoints() -> None:
    # A long continuous leg downsamples to the budget but keeps first + last fix.
    fixes = [_fix(i, lon=-105.0 + i * 0.001) for i in range(500)]
    trail = build_trail(fixes, max_points=50)
    leg = trail.legs[0]
    assert leg.fix_count == 500  # the count summarizes the full leg
    assert len(leg.points) <= 50  # but the drawn polyline is bounded
    assert leg.points[0] == [fixes[0].latitude, fixes[0].longitude]
    assert leg.points[-1] == [fixes[-1].latitude, fixes[-1].longitude]


def test_distance_is_over_full_fixes_not_downsampled() -> None:
    # Distance must reflect the real path, computed before downsampling thins it.
    fixes = [_fix(i, lon=-105.0 + i * 0.0001) for i in range(200)]
    dense = build_trail(fixes, max_points=10000)
    sparse = build_trail(fixes, max_points=10)
    assert abs(dense.total_distance_m - sparse.total_distance_m) < 1.0


def test_view_data_has_coordinates_only_in_leg_points() -> None:
    gap_min = DEFAULT_MAX_GAP_SECONDS / 60 + 10
    fixes = [_fix(0, battery=80, accuracy=5), _fix(1, battery=78)]
    fixes += [_fix(gap_min, battery=60), _fix(gap_min + 1, battery=58)]
    data = trail_view_data(build_trail(fixes), timezone="America/Denver")
    assert data["timezone"] == "America/Denver"
    assert len(data["legs"]) == 2 and len(data["gaps"]) == 1
    # Each leg carries ISO times + counts + a numeric distance, plus its points.
    leg = data["legs"][0]
    assert set(leg) == {"points", "fix_count", "started_at", "ended_at", "distance_m"}
    assert all(len(p) == 2 for p in leg["points"])  # [lat, lon] pairs
    # The gap row is times + duration only — no coordinate.
    gap = data["gaps"][0]
    assert set(gap) == {"after_leg", "started_at", "ended_at", "seconds"}
    assert "lat" not in gap and "points" not in gap
