"""Pure shaping of an ordered fix list into the `location_map` view payload.

This is the single place coordinates become render-only map data, so no tool
hand-rolls coordinate text (the plan's "coordinates-to-UI only" rule). It is
deliberately pure (no DB, no ctx, no clock): given fixes oldest-first, it splits
the trail into separate legs at any gap wider than the max-gap threshold — never
bridging a gap, so a GPS hole is honestly two polylines, not one straight line
across unknown territory — downsamples each leg so the whole payload stays under
a bounded point count, and emits the gap/segment metadata Option B's segments
list needs (per-leg start/end times + distance, and the gap duration between
legs). Coordinates appear ONLY in each leg's `points`; the textual segment rows
carry times/distances, never lat/lon.
"""

from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt

from jbrain.locations import FixPoint

# A gap wider than this between consecutive fixes ends a leg: the route across it
# is unknown (no signal / device off), so the trail must not be drawn across it.
DEFAULT_MAX_GAP_SECONDS = 30 * 60.0
# The whole payload is bounded to this many coordinates so a wide window can never
# stream the hypertable into the bubble; each leg gets a share proportional to its
# size, with a floor so a tiny leg keeps its endpoints.
DEFAULT_MAX_POINTS = 2000
_MIN_LEG_POINTS = 2
_EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class TrailLeg:
    """One continuous leg of a trail (no gap inside it): its downsampled points as
    [lat, lon] pairs, the fix-count it summarizes, its time span, and the
    great-circle distance walked. The map draws `points` as one polyline."""

    points: list[list[float]]
    fix_count: int
    started_at: datetime
    ended_at: datetime
    distance_m: float


@dataclass(frozen=True)
class TrailGap:
    """The unknown span between two legs: when signal was lost and regained and how
    long the hole was. The segments list renders this as a "no signal" row; the map
    never draws across it."""

    after_leg: int
    started_at: datetime
    ended_at: datetime
    seconds: float


@dataclass(frozen=True)
class Trail:
    """The full gap-split, downsampled trail: the legs (each a polyline) and the
    gaps between them, plus the totals the prose summary names. `is_empty` flags a
    window with no fixes so the caller can answer "no trail" without a view."""

    legs: list[TrailLeg]
    gaps: list[TrailGap]
    total_fixes: int
    total_distance_m: float

    @property
    def is_empty(self) -> bool:
        return self.total_fixes == 0


def _haversine_m(a: FixPoint, b: FixPoint) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(min(1.0, sqrt(h)))


def _split_into_legs(fixes: list[FixPoint], max_gap_seconds: float) -> list[list[FixPoint]]:
    """Cut the ordered fixes wherever consecutive captures are more than
    `max_gap_seconds` apart — each cut starts a new leg."""
    legs: list[list[FixPoint]] = []
    current: list[FixPoint] = []
    for fix in fixes:
        gap = (fix.captured_at - current[-1].captured_at).total_seconds() if current else 0.0
        if current and gap > max_gap_seconds:
            legs.append(current)
            current = []
        current.append(fix)
    if current:
        legs.append(current)
    return legs


def _downsample(fixes: list[FixPoint], budget: int) -> list[FixPoint]:
    """Keep at most `budget` fixes, evenly spaced, always retaining the first and
    last so a leg's endpoints (and thus its drawn extent) survive. A leg already
    within budget is returned unchanged."""
    n = len(fixes)
    if n <= budget or budget < _MIN_LEG_POINTS:
        return fixes
    if budget == n:
        return fixes
    # Evenly-spaced indices across [0, n-1] inclusive, so first and last are kept.
    step = (n - 1) / (budget - 1)
    picked = sorted({round(i * step) for i in range(budget)})
    return [fixes[i] for i in picked]


def _leg_distance(fixes: list[FixPoint]) -> float:
    return sum(_haversine_m(fixes[i - 1], fixes[i]) for i in range(1, len(fixes)))


def build_trail(
    fixes: list[FixPoint],
    *,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    max_points: int = DEFAULT_MAX_POINTS,
) -> Trail:
    """Turn an ordered (oldest-first) fix list into a gap-split, downsampled
    `Trail`. Distance is computed over the FULL fixes (before downsampling) so the
    summary is honest; downsampling only thins the drawn polyline. The point budget
    is divided across legs in proportion to their size, with a per-leg floor so a
    small leg keeps both endpoints."""
    if not fixes:
        return Trail(legs=[], gaps=[], total_fixes=0, total_distance_m=0.0)

    raw_legs = _split_into_legs(fixes, max_gap_seconds)
    total_fixes = len(fixes)
    legs: list[TrailLeg] = []
    for raw in raw_legs:
        # Proportional budget, but never below the floor (so endpoints survive) and
        # never above the leg's own size.
        share = max(_MIN_LEG_POINTS, round(max_points * len(raw) / total_fixes))
        kept = _downsample(raw, min(share, len(raw)))
        distance = _leg_distance(raw)
        legs.append(
            TrailLeg(
                points=[[f.latitude, f.longitude] for f in kept],
                fix_count=len(raw),
                started_at=raw[0].captured_at,
                ended_at=raw[-1].captured_at,
                distance_m=distance,
            )
        )

    gaps: list[TrailGap] = []
    for i in range(1, len(raw_legs)):
        prev_end = raw_legs[i - 1][-1].captured_at
        next_start = raw_legs[i][0].captured_at
        gaps.append(
            TrailGap(
                after_leg=i - 1,
                started_at=prev_end,
                ended_at=next_start,
                seconds=(next_start - prev_end).total_seconds(),
            )
        )

    return Trail(
        legs=legs,
        gaps=gaps,
        total_fixes=total_fixes,
        total_distance_m=sum(leg.distance_m for leg in legs),
    )


def trail_view_data(trail: Trail, *, timezone: str | None = None) -> dict:
    """The `ViewPayload.data` for the `location_map` view. Render-only: the only
    coordinates are inside each leg's `points`; everything else is times/counts/
    distances. `timezone` is the owner's IANA zone the client localizes against
    (the component formats the ISO timestamps); legs/gaps carry ISO times so the
    segments list reads in the owner's clock."""
    return {
        "timezone": timezone,
        "total_fixes": trail.total_fixes,
        "total_distance_m": round(trail.total_distance_m),
        "legs": [
            {
                "points": leg.points,
                "fix_count": leg.fix_count,
                "started_at": leg.started_at.isoformat(),
                "ended_at": leg.ended_at.isoformat(),
                "distance_m": round(leg.distance_m),
            }
            for leg in trail.legs
        ],
        "gaps": [
            {
                "after_leg": gap.after_leg,
                "started_at": gap.started_at.isoformat(),
                "ended_at": gap.ended_at.isoformat(),
                "seconds": round(gap.seconds),
            }
            for gap in trail.gaps
        ],
    }
