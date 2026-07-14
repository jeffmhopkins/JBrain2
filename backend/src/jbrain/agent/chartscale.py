"""Shared Y-scale helper for the chart tool-views (docs/plans/CHAT_CHARTS_PLAN.md).

Pure and dependency-free so both the lab plot (`labtools.lab_chart_view`) and the
generic charts (`charttools`) pick a clean axis the same way. Kept out of those
modules so neither imports the other.
"""

from __future__ import annotations

import math


def nice_scale(
    values: list[float], floor_at: float | None = None
) -> tuple[float, float, list[float]]:
    """A clean Y scale (min, max, inner ticks) around the data — ~5 intervals on a
    1/2/2.5/5 × 10ⁿ step. `floor_at` (e.g. a reference low) is kept in view so a band
    edge shows. Guarantees a strictly positive span even for an all-equal series."""
    lo = min(values)
    hi = max(values)
    if floor_at is not None:
        lo = min(lo, floor_at)
    rng = (hi - lo) or (abs(hi) or 1.0)
    lo_p = lo - rng * 0.2
    hi_p = hi + rng * 0.2
    raw = (hi_p - lo_p) / 5 or 1.0
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = next((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), 10 * mag)
    y_min = math.floor(lo_p / step) * step
    y_max = math.ceil(hi_p / step) * step
    if y_max <= y_min:  # a degenerate span (shouldn't happen after padding) — force one step
        y_max = y_min + step
    ticks: list[float] = []
    t = y_min + step
    while t < y_max - 1e-9:
        ticks.append(round(t, 4))
        t += step
    if all(float(v).is_integer() for v in (y_min, y_max, *ticks)):
        return int(y_min), int(y_max), [int(v) for v in ticks]
    return y_min, y_max, ticks
