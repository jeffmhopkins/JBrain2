"""What is on the air in one waterfall row: signals, not bins.

The live twin of `sweep.py`'s `steady` — and a SIBLING rather than an import, because
the two run in different containers and this one is apt-only (no pip, `Dockerfile.sdr`).
The rules it mirrors are named where they are used, so the two can be compared by eye:

  a bin is judged against its NEIGHBOURS, never against the whole span  (`_local_floors`)
  adjacent bins are one signal, grouped by adjacency and not snapped    (`_group`)

What it deliberately does NOT mirror is `occupancy`, which is the fraction of a sweep's
intervals a bin spent above its floor. A live row has no intervals — it IS one — so the
question "how much of the time" cannot be asked here and is not answered with a number
that looks like it was. What a row can say is: this stands above the noise around it, by
this much, right now. Holding a signal across rows is the viewer's job, where the history
lives.
"""

from __future__ import annotations

import math
from typing import Any

#: How far above its neighbourhood a bin must stand to be a signal. `sweep.py` reaches
#: the same judgement with `STEADY_DB = 6.0` against a floor taken over time; a single
#: row has only itself, so this is deliberately a little stricter — a row's own noise
#: wanders more than a quarter-percentile floor does.
SNR_DB = 8.0
#: Enough band that no single channel can move the median. Both figures are `sweep.py`'s,
#: for the reasons written there: 400 kHz spans many narrowband VHF/UHF channels, and 21
#: channels keeps one signal under 5% of its own window on a band whose channels are wide.
BASELINE_SPAN_HZ = 400_000
BASELINE_CHANNELS = 21
#: Fewest bins a baseline may be built from, so a coarse row cannot let a signal three
#: bins wide become most of its own baseline and hide itself.
BASELINE_MIN_BINS = 11
#: The most signals one row will report. A row with more than this in it is a band that
#: wants looking at rather than a list that wants reading, and the cap bounds both the
#: frame every viewer receives and the work done per row.
MAX_PEAKS = 24


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return math.nan
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _local_floors(db: list[float], bin_hz: float, channel_hz: int) -> list[float]:
    """Each bin's neighbourhood level: the median of the levels around it.

    `sweep.py`'s reasoning, unchanged: "higher than it has any business being" only has
    an answer relative to somewhere, and a span-wide median is the wrong somewhere —
    band-edge rolloff drags the ends down and each hop of a stitched row has its own
    noise. A window wider than the row degrades to a global median rather than
    misbehaving, because the slicing already does that."""
    span = max(BASELINE_SPAN_HZ, BASELINE_CHANNELS * max(channel_hz, 0))
    width = max(int(span // bin_hz) if bin_hz > 0 else 0, BASELINE_MIN_BINS)
    half = width // 2
    return [
        _median([v for v in db[max(0, i - half) : i + half + 1] if math.isfinite(v)])
        for i in range(len(db))
    ]


def find(
    db: list[float],
    start_hz: float,
    bin_hz: float,
    *,
    channel_hz: int = 0,
    snr_db: float = SNR_DB,
    limit: int = MAX_PEAKS,
) -> list[dict[str, Any]]:
    """The signals in one row, strongest first.

    Each is `{hz, db, over_db}`: where it is, how strong it is, and how far it stands
    above the noise around it — which is the number that decides whether it is a signal
    at all, so it travels with it rather than being recoverable only by someone holding
    the whole row.

    A bin that measured nothing is not a quiet bin (a hop that lost a block leaves NaN),
    and letting one through poisons every comparison: it is skipped rather than floored,
    the same choice `sweep.py` makes for the same reason."""
    if bin_hz <= 0 or not db:
        return []
    floors = _local_floors(db, bin_hz, channel_hz)
    over: list[tuple[int, float, float]] = []
    for index, value in enumerate(db):
        floor = floors[index]
        if not math.isfinite(value) or not math.isfinite(floor):
            continue
        excess = value - floor
        if excess >= snr_db:
            over.append((index, value, excess))
    if not over:
        return []

    # Grouped by ADJACENCY and not snapped to a grid, for `sweep.py`'s reason: real band
    # plans are not anchored at 0 Hz, so two bins either side of a boundary would be
    # reported as two signals by a rule that rounds. `channel_hz` zero means the caller
    # did not say, and then only touching bins are the same signal — a 16 kHz
    # transmission in a 9 kHz row would otherwise read as several stations a few kHz
    # apart, which is not a thing that happens.
    apart = max(1, int(max(channel_hz, 0) // bin_hz) or 1)
    signals: list[tuple[int, float, float]] = []
    cluster: list[tuple[int, float, float]] = []
    for entry in over:
        if cluster and entry[0] - cluster[-1][0] > apart:
            signals.append(max(cluster, key=lambda e: e[1]))
            cluster = []
        cluster.append(entry)
    if cluster:
        signals.append(max(cluster, key=lambda e: e[1]))

    signals.sort(key=lambda e: -e[1])
    return [
        {
            "hz": round(start_hz + index * bin_hz, 1),
            "db": round(value, 1),
            "over_db": round(excess, 1),
        }
        for index, value, excess in signals[:limit]
    ]
