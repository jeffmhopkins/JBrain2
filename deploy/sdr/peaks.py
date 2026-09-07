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
#: How close two SEPARATE signals may be, as a share of the channel raster. The same 0.6
#: `frontend/src/sdrPeaks.ts` holds signals together with, and deliberately the same: the
#: browser folds this list across rows using that number, so a box that split more finely
#: than the client merges would send pills the client then has to un-split.
#:
#: 120 kHz on the FM dial covers a broadcast carrier's loudest bin wandering across its
#: own 180 kHz width; 15 kHz on the 2 m plan's 25 kHz channels.
SAME_SIGNAL_SHARE = 0.6


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


#: How many signals a row needs before its grid PHASE is worth estimating. Three peaks
#: can agree on a spacing by coincidence; a dial-full cannot.
MIN_RASTER_PEAKS = 4
#: How tightly they must agree, as the resultant length of the phases taken as unit
#: vectors — 1.0 is every signal on the same grid, 0.0 is scattered.
#:
#: **Flat, and there is a regime where flat is wrong.** A peak sits on a bin centre, so
#: signals exactly on grid still arrive quantised, and how much that scatters their phase
#: depends on how coarse a bin is against the channel. MEASURED, for stations perfectly
#: on grid:
#:
#:     bin/channel   0.05   0.19   0.38   0.44   0.56
#:     resultant     0.998  0.967  0.859  0.652  0.445
#:
#: Past about 0.44 a perfect grid cannot clear 0.7, and this would abstain on it. Every
#: pairing this box actually uses is 0.05-0.38 — 9.4 kHz bins against rasters of 200,
#: 50 and 25 kHz — so the case is unreachable, and a normalisation for it was built,
#: measured, and taken out again rather than shipped untested. The number is here so the
#: next coarse sweep of a narrow raster is a known limit rather than a surprise.
MIN_RASTER_AGREEMENT = 0.7
#: How far off its channel a signal may sit and still be called that channel. Beyond half
#: a raster it is nearer the next one; 0.35 leaves a margin where nothing is claimed.
MAX_RASTER_PULL = 0.35
#: The only two places a real band plan anchors its grid: on the round number, or half a
#: channel off it. Airband is 25 kHz from 118.000 and NOAA 25 kHz from 162.400 (both
#: zero); US FM is 200 kHz sitting on 88.1 against a band that starts at 88.0, and
#: European FM 100 kHz from 87.5 (both half). Nothing in the wild anchors a third of a
#: channel along.
RASTER_ANCHORS = (0.0, 0.5)
#: How near an anchor the measured phase must land to be called that anchor.
MAX_ANCHOR_ERROR = 0.15


def raster_origin(freqs: list[float], channel_hz: int) -> float | None:
    """Where the channel grid actually sits, measured from the signals themselves.

    **The spacing is in the band plan; the ORIGIN is not, and assuming one is how a
    whole dial ends up labelled 100 kHz wrong.** US FM is 200 kHz spaced and sits on
    88.1, 88.3, ... — half a channel off the 88.0 the band starts at — while airband is
    25 kHz spaced and sits ON 118.000. Neither offset is recorded anywhere, and there is
    no rule that derives both.

    So it is measured. Each signal's position within one channel is a PHASE, and phases
    are circular — 199 kHz and 1 kHz are 2 kHz apart, not 198 — so they are averaged as
    unit vectors. The resultant length says how much the signals agree: a dial of real
    stations lands them almost on top of each other, and a band with no grid scatters
    them evenly and cancels.

    The average is then RESOLVED to one of `RASTER_ANCHORS` rather than used directly,
    because a raw average is a different number on every row and would give one station a
    different label each time — which is the defect this exists to fix, merely smaller.

    **Returns None when the signals do not agree, and when their agreement is not on any
    anchor a band plan uses** — the two reasons this is a measurement and not a
    formula."""
    if channel_hz <= 0 or len(freqs) < MIN_RASTER_PEAKS:
        return None
    phases = [2.0 * math.pi * (hz % channel_hz) / channel_hz for hz in freqs]
    x = sum(math.cos(a) for a in phases) / len(phases)
    y = sum(math.sin(a) for a in phases) / len(phases)
    if math.hypot(x, y) < MIN_RASTER_AGREEMENT:
        return None
    phase = (math.atan2(y, x) % (2.0 * math.pi)) / (2.0 * math.pi)
    # MEASURED, then CHECKED against what band plans actually do — and this second step is
    # what makes the label hold still. The raw estimate carries the residual of whichever
    # peaks this row happened to see, so two rows of the same dial land on grids a few
    # kHz apart; snapping to them gives one station two labels again, just closer
    # together. Every real plan anchors on the round number or half a channel off it, so
    # the estimate is resolved to whichever it is near, and to NEITHER when it is near
    # neither. Two rows then agree exactly, because they are agreeing on a convention
    # rather than on an average.
    for anchor in RASTER_ANCHORS:
        away = abs((phase - anchor + 0.5) % 1.0 - 0.5)
        if away <= MAX_ANCHOR_ERROR:
            return anchor * channel_hz
    return None


def _snapped(hz: float, origin: float | None, channel_hz: int) -> float | None:
    """The channel this signal is in, or None when it is not close enough to one."""
    if origin is None:
        return None
    channel = round((hz - origin) / channel_hz) * channel_hz + origin
    return channel if abs(hz - channel) <= MAX_RASTER_PULL * channel_hz else None


#: How far a carrier must stand above the dips on BOTH sides of it to be its own signal.
#:
#: Topographic prominence, which is what a spectrum analyser calls "peak excursion" —
#: Keysight's SA mode ships 6 dB by default. It is the one mainstream instrument test
#: this file did not have, and it is the test that separates a station from the SKIRT of
#: the station next door: a skirt is monotonic, so it has no dip on its inboard side and
#: no prominence, however far it stands above the noise floor.
#:
#: MEASURED on a 45 s integration of the owner's own dial: 21 real stations, and 16
#: surplus entries he called noise. At 2.0 dB with the width test below, 21 of 21 real
#: stations are kept and 15 of the 16 surplus are rejected. The surplus that survives is
#: one splatter shoulder at 105.9.
MIN_PROMINENCE_DB = 2.0

#: How wide a real emission is, as a share of its channel — and a floor in bins.
#:
#: The other half of the same measurement. The surplus that prominence alone does not
#: catch is a family of BIRDIES: 19-28 kHz spikes, two or three bins wide, with
#: row-to-row variation of 0.25 dB where the band median is 0.29. Something that narrow
#: and that steady is not a modulated emission — it is the receiver.
#:
#: A share of the channel rather than a constant, because "wide" means nothing without
#: the band: 35 kHz is a fifth of an FM channel and more than a whole NOAA one. The bin
#: floor is what applies where a band has no raster, or where its channels are barely
#: wider than the transform's own resolution — there, three bins is the least that can
#: have a shape at all.
MIN_WIDTH_SHARE = 0.175
MIN_WIDTH_BINS = 3

#: How far out the flanking dips are looked for. Bounded, exactly as `scipy`'s
#: `peak_prominences` bounds it with `wlen`: prominence measured to the horizon is a
#: statement about the whole band, and what is wanted here is whether this signal is
#: separate from ITS NEIGHBOURS.
#:
#: The bin floor has to clear the WIDEST signal the row can hold, because the search
#: walks across a flat top before it can reach the ground either side. At 12 it did not:
#: a rectangular 24-bin carrier measured ZERO prominence and was rejected, because the
#: walk ran out of reach still standing on the signal. Being generous costs nothing —
#: the search stops the moment it meets a bin higher than the peak, which is exactly
#: what makes a skirt score zero.
PROMINENCE_REACH_SHARE = 0.8
PROMINENCE_REACH_BINS = 32


def shape_of(
    db: "Sequence[float]", index: int, bin_hz: float, channel_hz: int
) -> tuple[float, float]:
    """How far this bin stands above the dips either side, and how wide it is.

    Returns `(prominence_db, width_hz)`. Prominence is the peak less the HIGHER of the
    two flanking minima — the standard topographic definition, and the higher one is
    what matters: a signal is only separate from its neighbours if it is separate from
    both. Width is measured at half prominence, which is where the shoulders of a
    carrier are rather than where the noise happens to cross a threshold.

    Non-finite bins are treated as the end of the search rather than as dips: a bin that
    measured nothing cannot be evidence that a signal is isolated."""
    total = len(db)
    if total == 0 or not 0 <= index < total:
        return 0.0, 0.0
    peak = db[index]
    if not math.isfinite(peak):
        return 0.0, 0.0
    reach = max(
        PROMINENCE_REACH_BINS,
        int(round(PROMINENCE_REACH_SHARE * channel_hz / bin_hz)) if bin_hz > 0 else 0,
    )

    def dip(step: int) -> float:
        """The lowest level between here and the next thing at least as high."""
        low = peak
        at = index + step
        while 0 <= at < total and abs(at - index) <= reach:
            value = db[at]
            if not math.isfinite(value):
                break
            if value > peak:
                break  # a higher neighbour bounds the search, as `wlen` does
            low = min(low, value)
            at += step
        return low

    prominence = peak - max(dip(-1), dip(1))
    # At HALF prominence: the level where a carrier's own shoulders are. Measuring at
    # the detection threshold instead would make the width depend on the noise floor.
    edge = peak - prominence / 2.0
    left = index
    while left - 1 >= 0 and math.isfinite(db[left - 1]) and db[left - 1] >= edge:
        left -= 1
    right = index
    while right + 1 < total and math.isfinite(db[right + 1]) and db[right + 1] >= edge:
        right += 1
    return prominence, (right - left + 1) * bin_hz


def min_width_hz(bin_hz: float, channel_hz: int) -> float:
    """The narrowest thing that can be a real emission on this band."""
    return max(MIN_WIDTH_BINS * bin_hz, MIN_WIDTH_SHARE * channel_hz)


#: How far a channel must stand above the TROUGHS either side of it, in dB.
#:
#: **MEASURED AND NOT WIRED IN.** `_has_shape` does not consult this yet, and the reason
#: is a second measurement: on a 45 s integration it separates perfectly (21 of 21 real
#: stations kept, 16 of 16 surplus rejected, beating prominence+width which leaves one),
#: but the worst real station clears by only 1.06 dB while the best artefact reaches
#: 0.33 — 0.73 dB of daylight. On a SHORT window that gap closes and inverts: measured
#: on single max-of-4 rows, the worst real station scored -0.14 dB and the best surplus
#: +1.50. The decision row here is a mean of `HOLD_SWEEPS` sweeps, about 1.8 s, which is
#: nearer the short case than the long one. Wiring it in at this window would delete real
#: stations to remove two artefacts, which is the wrong trade.
#:
#: What it needs is a decision window of ~7 s, separate from the picture's. That is a
#: real change with a real cost — a burst on a scanner band would take that long to be
#: reported — so it is a decision to take deliberately rather than a constant to flip.
#:
#: The last test, and the one that catches what prominence and width leave: a first
#: adjacent that is genuinely a skirt. The owner named the family — "the peaks are
#: sometimes sidebands" — and 47 CFR 73.207 minimum separations say two LOCAL
#: first-adjacent stations essentially cannot exist, so a run of consecutive occupied
#: channels is one station and its skirts.
#:
#: **Measured against the troughs, not against the neighbouring CHANNEL.** The obvious
#: rule — reject a channel weaker than its neighbour — cannot work, and not for want of
#: tuning: for any adjacent pair the two margins sum to exactly zero, so it can never
#: report BOTH members of a pair. Proved over all 99 pairs on the owner's dial, where it
#: would have deleted 101.3, which is real (a 3.17 dB notch separates it from 101.1,
#: against 0.09-0.50 dB for the splatter pairs). The trough reference does not have that
#: antisymmetry: two real neighbours both rise above the dip between them.
#:
#: MEASURED on the 45 s integration: the worst of the 21 real stations clears by 1.29 dB
#: and the best of the 16 surplus reaches 0.60 dB. Anywhere in 0.7-1.2 gives the same
#: answer; 0.9 is the middle of that.
MIN_GUARD_MARGIN_DB = 0.9

#: The channel's own half-width, and where the troughs either side are looked for, as
#: shares of the raster. Chosen by grid search over the real data: 0.25/0.5/0.25 —
#: a +-50 kHz channel and +-50 kHz guards centred 100 kHz out on a 200 kHz plan.
GUARD_CHANNEL_SHARE = 0.25
GUARD_OFFSET_SHARE = 0.5

#: The bins per channel below which the guard test is not asked at all.
#:
#: The guard bands sit half a channel out and are half a channel wide, so a plan whose
#: channels are only a few bins across has guards barely one bin wide, landing inside
#: the neighbouring carriers' skirts — the margin then measures nothing. On the FM dial
#: a channel is 21 bins; on a 25 kHz plan at the same resolution it is under three, and
#: this test must stay out of the way there. It is also why adjacent-occupancy bands —
#: 2 m repeater pairs, 25 kHz airband, where two neighbours at once is the DESIGN — are
#: never judged by it.
MIN_GUARD_BINS_PER_CHANNEL = 8


def _band_power(db: "Sequence[float]", centre: int, half: int) -> float:
    """Mean power across a span of bins, in dB. Linear mean, because a mean of
    logarithms is a geometric mean and not the average power of anything."""
    total = 0.0
    count = 0
    for at in range(max(0, centre - half), min(len(db), centre + half + 1)):
        value = db[at]
        if math.isfinite(value):
            total += 10.0 ** (value / 10.0)
            count += 1
    if count == 0:
        return math.nan
    return 10.0 * math.log10(total / count)


def guard_margin_db(
    db: "Sequence[float]", index: int, bin_hz: float, channel_hz: int
) -> float:
    """How far this channel stands above the quieter of the two troughs beside it.

    NaN when the band plan cannot support the question — see
    `MIN_GUARD_BINS_PER_CHANNEL` — so a caller can tell "not asked" from "failed"."""
    if channel_hz <= 0 or bin_hz <= 0:
        return math.nan
    if channel_hz / bin_hz < MIN_GUARD_BINS_PER_CHANNEL:
        return math.nan
    half = max(1, int(round(GUARD_CHANNEL_SHARE * channel_hz / bin_hz)))
    offset = max(1, int(round(GUARD_OFFSET_SHARE * channel_hz / bin_hz)))
    here = _band_power(db, index, half)
    low = _band_power(db, index - offset, half)
    high = _band_power(db, index + offset, half)
    beside = [side for side in (low, high) if math.isfinite(side)]
    if not math.isfinite(here) or not beside:
        return math.nan
    return here - max(beside)


def _has_shape(
    db: "Sequence[float]", index: int, bin_hz: float, channel_hz: int, floor_hz: float
) -> bool:
    prominence, width = shape_of(db, index, bin_hz, channel_hz)
    return prominence >= MIN_PROMINENCE_DB and width >= floor_hz


def find(
    db: list[float],
    start_hz: float,
    bin_hz: float,
    *,
    channel_hz: int = 0,
    snr_db: float = SNR_DB,
    limit: int = MAX_PEAKS,
    origin: float | None = None,
    shape: bool = True,
) -> list[dict[str, Any]]:
    """The signals in one row, strongest first.

    Each is `{hz, measured_hz, db, over_db}`: which channel it is in, where its energy
    actually peaked, how strong it is, and how far it stands above the noise around it —
    which is the number that decides whether it is a signal at all, so it travels with it
    rather than being recoverable only by someone holding the whole row.

    `hz` is snapped to the channel grid when `channel_hz` is given and a grid is known;
    otherwise it is the measured peak and equals `measured_hz`.

    `origin` is a grid ALREADY ESTABLISHED — pass the one a previous row settled on and
    this row uses it rather than re-deriving. That is what a caller holding a session
    should do, because deriving per row is not stable: a row whose signals happen not to
    agree (`MIN_RASTER_AGREEMENT`) reports raw measurements, the next row snaps them, and
    the viewer holding peaks across rows sees ONE station arrive twice under two labels.
    Measured on the FM dial: 50 held signals for 21 on the air, most of the surplus being
    the same stations wearing both a snapped and an unsnapped name.

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

    # TWO rules, because they answer two different questions (B4).
    #
    # **Adjacency**, first, and not snapped to a grid — for `sweep.py`'s reason: real
    # band plans are not anchored at 0 Hz, so two bins either side of a boundary would be
    # reported as two signals by a rule that rounds. A run is broken only by a gap wider
    # than `MIN_FOLD_BINS`, which is what stops noise dipping one bin inside a carrier
    # from splitting it in two.
    runs: list[list[tuple[int, float, float]]] = []
    cluster: list[tuple[int, float, float]] = []
    for entry in over:
        if cluster and entry[0] - cluster[-1][0] > MIN_FOLD_BINS:
            runs.append(cluster)
            cluster = []
        cluster.append(entry)
    if cluster:
        runs.append(cluster)

    # **Then the raster, applied to a run's WIDTH** — which is where the old rule was
    # wrong (B4). It asked whether the GAP between two above-threshold stretches was
    # wider than a whole channel, and two stations one raster apart never have such a
    # gap: each is most of a channel wide, and what separates their skirts is the
    # remainder. MEASURED: two FM stations 200 kHz apart came back as ONE signal, which
    # is the failure where a band reads emptier than it is.
    #
    # A run 380 kHz wide on a 200 kHz raster is not one station with a carrier twice the
    # legal width; it is two. So a run holds `round(width / channel)` of them, and each
    # equal share's strongest bin is where one is — the division puts its boundaries at
    # the midpoints between stations, which is exactly where the skirts meet.
    signals: list[tuple[int, float, float]] = []
    for run in runs:
        width_hz = (run[-1][0] - run[0][0] + 1) * bin_hz
        parts = max(1, round(width_hz / channel_hz)) if channel_hz > 0 else 1
        if parts == 1:
            signals.append(max(run, key=lambda e: e[1]))
            continue
        span = len(run) / parts
        for part in range(parts):
            piece = run[int(part * span) : int((part + 1) * span)]
            if piece:
                signals.append(max(piece, key=lambda e: e[1]))

    signals.sort(key=lambda e: -e[1])
    # ...and the same raster the other way, to put back what ADJACENCY oversplits. A
    # carrier with a deep notch in it arrives as two runs, each well under a channel
    # wide, and two fragments 90 kHz apart on a 200 kHz raster are one station. 0.6 is
    # the share `frontend/src/sdrPeaks.ts` already holds signals together with, and it is
    # the same quantity — a box that split more finely than the client merges would send
    # pills the client then has to un-split. Strongest first, so the survivor is the one
    # that was actually louder.
    apart_bins = SAME_SIGNAL_SHARE * channel_hz / bin_hz if channel_hz > 0 else 0.0
    if apart_bins > 0:
        kept: list[tuple[int, float, float]] = []
        for entry in signals:
            if all(abs(entry[0] - other[0]) >= apart_bins for other in kept):
                kept.append(entry)
        signals = kept
    # **SHAPE, last, and it is what makes a station different from its neighbour's
    # skirt.** Everything above asks how far a bin stands above the noise; nothing above
    # asks whether it is a bump at all. A skirt clears any threshold — it is the side of
    # a loud carrier — and has no dip on its inboard side, so it has no prominence. A
    # receiver birdie has prominence and is three bins wide.
    #
    # MEASURED against a 45 s integration of the owner's dial (21 real stations, 16
    # entries he called noise): this pair keeps 21 of 21 and rejects 15 of 16, where
    # raising the threshold instead keeps only 18 of 21 and capping the count keeps 19.
    # The two families it separates are the two he described — "sidebands, or multiple
    # hits that are very close together".
    if shape:
        # WIDTH ONLY WHERE A BAND PLAN SAYS WHAT WIDE MEANS. Prominence is a question
        # about shape and needs no plan; width is a question about bandwidth and cannot
        # be asked without one. With no raster a signal may legitimately be one bin, and
        # a floor guessed from the transform's own resolution would delete it.
        floor_hz = min_width_hz(bin_hz, channel_hz) if channel_hz > 0 else 0.0
        signals = [
            entry
            for entry in signals
            if _has_shape(db, entry[0], bin_hz, channel_hz, floor_hz)
        ]
    shown = signals[:limit]
    # NO HALF-BIN HERE, and it is worth saying why, because it was added once and had to
    # come back out. `iq.Spectrometer.start_hz` is bin 0's CENTRE — `fftshift` puts DC in
    # bin `n // 2` — so `start_hz + index * bin_hz` already addresses bin `index` exactly,
    # which is what `Spectrum.stop_hz` says and what `server._channel_centre` assumes
    # (C20 exists because those two once sat half a bin apart).
    #
    # An analysis of an `rtl_power` CSV found its grid 5.14 kHz off the channels under
    # the edge convention and 0.45 kHz under the centre one — but that is a fact about
    # rtl_power's `hz_low`, which IS a band edge, and not about this path.
    measured = [start_hz + index * bin_hz for index, _v, _e in shown]
    # SNAPPED TO THE CHANNEL, when the row's own signals agree there is one.
    #
    # A hopped row samples each slice for a fraction of a millisecond, and a wideband-FM
    # carrier's instantaneous spectrum swings across its own +-75 kHz of deviation — so
    # the argmax of one snapshot is where the modulation happened to be, not where the
    # station is. MEASURED ON THE DIAL: peaks landing -25.0, +15.6, +15.6, +3.1 kHz off
    # their channels, and 88.263 and 88.356 both reported for 88.3 — one station, two
    # pills, 93 kHz apart.
    #
    # On a band with a raster a signal IS a channel, so that is what it is called.
    # `measured_hz` rides alongside so the claim can be checked rather than believed:
    # a snapped number nobody can compare against what was seen is exactly the kind of
    # number this file exists not to produce.
    grid = raster_origin(measured, channel_hz) if origin is None else origin
    labelled = [
        {
            "hz": round(_snapped(hz, grid, channel_hz) or hz, 1),
            "measured_hz": round(hz, 1),
            "db": round(value, 1),
            "over_db": round(excess, 1),
            # The shape that got it past `_has_shape`, carried for `measured_hz`'s
            # reason: a signal kept or dropped on a rule nobody can check against the
            # measurement is exactly the kind of number this file exists not to produce.
            "prominence_db": round(shape_of(db, index, bin_hz, channel_hz)[0], 1),
            "width_hz": round(shape_of(db, index, bin_hz, channel_hz)[1], 1),
        }
        for hz, (index, value, excess) in zip(measured, shown, strict=True)
    ]
    return _one_per_channel(labelled)


def _one_per_channel(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per channel, keeping the strongest — order otherwise preserved.

    SNAPPING CAN COLLIDE, and nothing downstream can undo it. The adjacency rule breaks
    a run on a gap wider than `MIN_FOLD_BINS`, which is right for two stations a few
    bins apart and wrong for one wideband-FM carrier whose middle dips below the
    threshold in the row that happened to be measured: two runs, two peaks, both landing
    on the same channel once snapped. The viewer holds peaks by frequency, so it kept
    both — REPORTED as two pills reading 106.100 side by side, +23.2 and +22.3 dB.

    The strongest wins rather than the first, because "first" here means "leftmost bin",
    which is the shoulder of a carrier rather than its middle."""
    best: dict[float, dict[str, Any]] = {}
    for signal in signals:
        at = signal["hz"]
        held = best.get(at)
        if held is None or signal["db"] > held["db"]:
            best[at] = signal
    # Back into the order they were found in — strongest first, which is what every
    # caller reads them as.
    return [signal for signal in signals if best.get(signal["hz"]) is signal]
