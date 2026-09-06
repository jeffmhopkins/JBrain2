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
from collections.abc import Sequence
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

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
#: Where in the sorted neighbourhood the floor is read. NOT the median: a median assumes
#: the window is mostly noise, and `baseline_width`'s ceiling allows a signal to be up to
#: a third of its own reference. The 35th percentile still reads noise there, and costs a
#: fraction of a decibel where the window really is empty.
BASELINE_SHARE = 0.35
#: How many times per window the baseline is actually evaluated; between those points it
#: is interpolated. The baseline is slowly varying by construction, so this is an
#: approximation only in the sense that a straight line between two nearby samples of a
#: smooth function is: measured at 0.07-0.12 dB against the exact rolling percentile,
#: which is a hundredth of `SNR_DB`, for about a two-hundredth of the work.
BASELINE_STRIDE = 32
#: The most signals one row will report. A row with more than this in it is a band that
#: wants looking at rather than a list that wants reading, and the cap bounds both the
#: frame every viewer receives and the work done per row.
MAX_PEAKS = 24
#: The narrowest gap that may still be one signal, in bins, when no band plan says
#: otherwise. A real carrier is contiguous, but noise drops the odd bin inside it back
#: under the threshold, and a rule that split on one bin reported one station two or
#: three times — seen by the owner on the FM dial. Three bins is far narrower than any
#: channel and far wider than a bin of noise.
MIN_FOLD_BINS = 3


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return math.nan
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def baseline_width(bins: int, bin_hz: float, channel_hz: int) -> int:
    """How many bins each bin is judged against — CLAMPED TO THE ROW.

    The clamp is the fix, and its absence is a fault that reached the owner. The window
    could only grow: `max(400 kHz, 21 channels)` is wider than the whole row for any
    sweep under 400 kHz, for every 256 kS/s capture, and for any 200 kHz raster — and a
    window wider than the row is a GLOBAL median, which is the "floor measured from
    inside the signal" failure this file exists to avoid. Measured on air 2026-09-06: a
    162.3-162.7 sweep (400 kHz, so 128 bins against a 525 kHz window) missed NOAA on
    162.550 while it was the strongest bin in the row, 9.8 dB over its own median.

    A third of the row is the ceiling, so a signal can never be more than a third of its
    own reference no matter how the caller sizes the sweep."""
    span = max(BASELINE_SPAN_HZ, BASELINE_CHANNELS * max(channel_hz, 0))
    want = int(span // bin_hz) if bin_hz > 0 else 0
    ceiling = max(BASELINE_MIN_BINS, bins // 3)
    width = max(min(max(want, BASELINE_MIN_BINS), ceiling), 1)
    # Odd DOWNWARD. Rounding up would step back over the ceiling the line above just
    # applied, which on a 128-bin row is 43 against a limit of 42.
    return width - 1 if width % 2 == 0 and width > 1 else width


def _local_floors(
    db: "Sequence[float] | np.ndarray", bin_hz: float, channel_hz: int
) -> "np.ndarray":
    """Each bin's neighbourhood level: a LOW PERCENTILE of the levels around it.

    Two changes from the rolling median this was, and both are load-bearing.

    **It is vectorised, because the median was costing more than the radio.** A
    `sorted()` per bin is O(N*W) in Python and ran on the CAPTURE THREAD, between
    `read()` calls: measured 238 ms for a 4000-bin stare, 321 ms with a 25 kHz raster
    and 1910 ms on the FM dial, against a 100 ms frame budget. That is what throttled
    the wideband waterfall to a third of its rate and overflowed USB buffers — defeating
    the "the radio never looks away" property `iq.py` exists to guarantee, downstream of
    it. It also explains the 0.33 fps this project blamed on retune settle.

    The baseline is a SLOWLY VARYING function of frequency by construction, so it is
    evaluated every `width/BASELINE_STRIDE` bins and interpolated between. Measured
    against the exact rolling percentile: **0.07-0.12 dB of error, for 1.0-1.4 ms** —
    a hundredth of `SNR_DB`, and about two hundred times faster than computing it at
    every bin.

    **And it is the 35th percentile, not the 50th.** A median assumes the window is
    mostly noise; a lower percentile still reads noise when a signal fills a third of
    its own reference, which is exactly the case `baseline_width`'s ceiling now permits
    at worst. It costs a fraction of a decibel where the window really is empty."""
    values = np.asarray(db, dtype=np.float64)
    n = values.size
    if n == 0:
        return values
    width = min(baseline_width(n, bin_hz, channel_hz), n if n % 2 else n - 1) or 1
    half = width // 2
    # NaN is a bin that measured nothing (a hop that lost a block), and it must not
    # drag a neighbourhood down. Filled with the row's own median, which is neutral.
    clean = np.where(np.isfinite(values), values, np.nan)
    if np.isnan(clean).any():
        fill = np.nanmedian(clean) if not np.isnan(clean).all() else 0.0
        clean = np.where(np.isnan(clean), fill, clean)
    padded = np.pad(clean, half, mode="edge")
    windows = sliding_window_view(padded, width)
    stride = max(1, width // BASELINE_STRIDE)
    at = np.arange(0, n, stride)
    if at[-1] != n - 1:
        at = np.append(at, n - 1)
    rank = min(width - 1, max(0, int(BASELINE_SHARE * (width - 1))))
    sampled = np.partition(windows[at], rank, axis=-1)[:, rank]
    return sampled if stride == 1 else np.interp(np.arange(n), at, sampled)


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
    # Never below `MIN_FOLD_BINS`: `channel_hz` zero means the caller did not say, and
    # "only touching bins" is a rule that splits a carrier the moment noise dips one bin
    # inside it. The band plan widens this; it can no longer narrow it to nothing.
    apart = max(MIN_FOLD_BINS, int(max(channel_hz, 0) // bin_hz))
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
