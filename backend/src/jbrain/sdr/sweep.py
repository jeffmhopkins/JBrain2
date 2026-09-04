"""Turning a band sweep into something a person or a model can read.

`rtl_power` writes one CSV row per frequency block per integration interval:

    2026-09-03, 15:00:00, 144000000, 144005000, 5000, 12, -71.2, -98.4, ...

date, time, low Hz, high Hz, bin Hz, samples, then one dB figure per bin. A five-minute
sweep of 2m at 5 kHz is ~800 bins over ~150 intervals — 120,000 numbers, which is a
picture a person can read and a table a model can.

**Both, and for different readers.** The image is for the owner: at five minutes a
waterfall is ~2 s per row, so a ten-second transmission is several rows tall and plainly
visible. (That argument fails at a DAY, where 86,400 seconds compressed into ~1,500
pixel rows is 55 s per row and the burst is averaged away — which is why the day-long
survey will want events, not an image.) The occupancy table is for everything else,
because no model reads a frequency off a picture.

**The floor is estimated per bin, as a low percentile over the whole window.** Not a
mean, which the very signals being hunted pull upward; not one global figure, because
the floor at 145.130 genuinely differs from 146.940 thanks to spurs, and the tuner's own
DC spike sits at the centre of every retune block. A per-bin floor absorbs all of that
without needing to know where the artifacts are.

**Occupancy is a percentage of the window, not a peak in dB.** A one-off burst and a
channel busy half the hour have the same peak; only one of them is a busy channel.

**Anything held constant is invisible to that, so it is found by comparison with its
NEIGHBOURS instead.** A carrier up the whole window becomes its own floor and reads as
0% occupied — see `Reduced.steady`. Neighbours rather than the whole sweep because a
floor is a local fact: band-edge rolloff, spurs and each retune block's own noise all
vary across a sweep, and one median over the lot answers for none of them.

Pure and total: it parses text and returns numbers. No I/O, no radio, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

# How far above its own floor a bin has to sit to count as occupied for one interval.
# 8 dB rather than the 3 dB that feels generous: an 800-bin sweep over 150 intervals is
# 120,000 chances to be wrong, so a threshold that clears a couple of false positives per
# sweep is one that clears thousands per day.
DEFAULT_SNR_DB = 8.0
# Which percentile of a bin's own history counts as its noise floor.
FLOOR_PERCENTILE = 0.25
# How far a bin's own floor must sit above ITS NEIGHBOURS' before it counts as never
# having gone quiet. Small, because the comparison is local: 6 dB over the couple of
# hundred kHz either side is a lot, where 6 dB over a whole band is nothing.
STEADY_DB = 6.0
# How much neighbouring spectrum makes up a bin's local floor when the caller names no
# channel width. In Hz rather than bins because a bin is 7812 Hz on one sweep and 1000 Hz
# on the next, and what this wants is "enough band that no one channel moves the median"
# — a fact about band plans, not FFTs. 400 kHz is a NARROWBAND VHF/UHF figure: 2m at
# 15 kHz, 70cm and airband and marine at 25 kHz all fit many channels inside it.
BASELINE_SPAN_HZ = 400_000
# ...and how many channel widths make up that floor when the caller DOES name one, which
# is what carries this off the narrowband bands. A signal has to be a small minority of
# the window it is judged against or it becomes its own baseline and hides: at FM
# broadcast's 200 kHz spacing the flat 400 kHz above spans two channels, so in a city the
# median IS a station; a 5-20 MHz cellular carrier swallows the window whole and the
# baseline is then computed from inside the signal. At 21 channels one signal is under 5%
# of its own window and five adjacent busy ones still leave the median in noise.
BASELINE_CHANNELS = 21
# Fewest bins a local baseline may ever be built from. It binds at coarse resolution —
# a 100 kHz bin (the route's limit) puts only four bins in 400 kHz, and a signal three
# bins wide is then most of its own baseline, so it hides itself.
BASELINE_MIN_BINS = 11


@dataclass(frozen=True, slots=True)
class Bin:
    """One frequency bin, over the whole window."""

    hz: int
    floor_db: float
    peak_db: float
    occupancy: float
    """Fraction of intervals in which this bin sat above its own floor + threshold."""


@dataclass(frozen=True, slots=True)
class Reduced:
    rows: int
    """Intervals actually parsed — a partial sweep has fewer than asked for."""
    start_hz: int
    stop_hz: int
    bin_hz: int
    floor_db: float
    """The median of the per-bin floors: one number for "how quiet is it here"."""
    busy: list[Bin] = field(default_factory=list)
    """Bins that were occupied at all, busiest first."""
    steady: list[Bin] = field(default_factory=list)
    """Bins sitting far above their NEIGHBOURS' floor ALL the time.

    A per-bin floor makes a constant emitter invisible — it becomes its own floor, which
    is exactly right for the tuner's DC spike and its spurs, and exactly wrong for a
    repeater holding a carrier for the whole window. Occupancy statistics cannot tell
    those two apart, because they are the same measurement. So they are reported here
    rather than silently dropped, and naming which is which needs a second look at the
    channel — not more arithmetic on this sweep.

    Compared against the local floor, not the sweep's, because "high" is a local claim:
    a repeater is obvious next to 146.655 and unremarkable next to a whole band.

    THE THRESHOLD IS CALIBRATED, against FM broadcast — which is the answer to "sweep
    something that is actually transmitting", since those carriers are up 24/7 and the
    2m sweeps that first raised the question caught a band with nothing on it.

    Measured 2026-09-03 across four sweeps. A real transmitter clears its local floor by
    **+11 to +24 dB** (88-108, 13 stations); noise sits at p50 0.09 dB, and a QUIET 2m
    band's worst bin reached only +4.8. Sweeping `STEADY_DB` over the same CSVs:

        3 dB -> 18 stations, and the first false positive on the quiet band
        6 dB -> 13 stations, quiet band silent      <- here
        8 dB ->  9 stations
       10 dB ->  8 stations

    So 6 dB is not a compromise between the two failures, it is above one and below the
    other: every station the band has, nothing on a band with none, and 3 dB of margin
    to the first false positive. Raising it LOSES real stations, which was the intuition
    to check — a signal's skirts fall away smoothly from its peak, so a higher bar keeps
    the strong and drops the weak rather than sharpening anything."""
    uncovered: list[tuple[int, int]] = field(default_factory=list)
    """Half-open Hz spans this sweep did not measure, low to high.

    Not a defect report — a coverage statement, and the difference matters when the
    reader is deciding whether "nothing at 146.1" means quiet or unlooked-at. This box's
    144-148 run came back with 145.872-146.206 missing, a 342 kHz hole sitting straight
    across live repeater channels, and nothing in the response said so. Silence there
    was never evidence. (That hole was this module's own doing — see the block width
    above — and the same sweep now returns 1026 contiguous bins with nothing missing;
    the reader still needs to be told on the day one exists.)"""
    grid: list[list[float]] = field(default_factory=list)
    """dB per bin per interval, oldest first — what the waterfall draws."""
    bins: int = 0
    revisit_s: float = 0.0
    """Seconds between consecutive readings of one bin, median, off the CSV's own clock.

    The scale `occupancy` is a fraction OF, and the thing that decides whether it means
    anything: a 10-second transmission is six intervals at a 1 s revisit and rounding
    error at a 60 s one. Zero when the timestamps could not be read."""


def _percentile(values: list[float], fraction: float) -> float:
    """A percentile without numpy, which the api does not carry.

    Nearest-rank on a sorted copy. For a few hundred samples per bin the exactness of
    interpolation buys nothing a noise floor can tell apart."""
    if not values:
        return 0.0
    ordered = sorted(values)
    at = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[at]


def _local_floors(floors: list[float], bin_hz: int, channel_hz: int = 0) -> list[float]:
    """Each bin's neighbourhood floor: the median of the floors around it.

    The whole point of `steady` is "higher than it has any business being", and that
    question only has an answer relative to somewhere. A sweep-wide median is the wrong
    somewhere: band-edge rolloff drags the ends down, spurs sit where the hardware puts
    them, and each retune block has its own noise — measured on this box, two blocks of
    one 144-148 sweep sat 0.68 dB apart, which is small but is not zero and is not
    knowable in advance. A rolling median tracks the floor the receiver actually had at
    each frequency; a single number answers for no part of it.

    `channel_hz` is what carries this off the narrowband bands: the window has to be many
    channels wide, and a channel is 15 kHz on 2m and 10 MHz on a cellular downlink. Zero
    means the caller did not say, and the flat `BASELINE_SPAN_HZ` applies.

    A sweep narrower than one window needs no special case: every bin's window is then
    the whole sweep, which is exactly the one-figure fallback, and the slicing already
    does it — so a window asked to be wider than the sweep degrades to a global median
    rather than misbehaving. The window is counted in bins that MEASURED something, so a
    sweep with unmeasured holes in it reaches a little further in Hz than asked; a median
    does not care, and the alternative is no baseline across a gap."""
    span = max(BASELINE_SPAN_HZ, BASELINE_CHANNELS * max(channel_hz, 0))
    width = max(int(span // bin_hz) if bin_hz > 0 else 0, BASELINE_MIN_BINS)
    half = width // 2
    return [_percentile(floors[max(0, i - half) : i + half + 1], 0.5) for i in range(len(floors))]


def _uncovered(columns: list[tuple[int, list[float]]], bin_hz: int) -> list[tuple[int, int]]:
    """The Hz spans between the bins that exist, plus the bins that hold nothing.

    Two ways a sweep misses spectrum and one way to say so: rtl_power's blocks can leave
    a gap between them, and a block can hand back a bin of non-finite values. Both mean
    the same thing to a reader — this sweep is not evidence about that frequency.

    Walks in FREQUENCY order, which `columns` is not: its bins are grouped by retune
    block, and two blocks that overlap hand back a sequence that steps backwards at the
    seam. Sorted, the running edge is a high-water mark for free and adjacent spans
    merge into one; unsorted, an overlap invents a hole and the merge misfires."""
    spans: list[tuple[int, int]] = []

    def mark(low: int, high: int) -> None:
        if high <= low:
            return
        if spans and spans[-1][1] >= low:
            spans[-1] = (spans[-1][0], max(spans[-1][1], high))
        else:
            spans.append((low, high))

    expect: int | None = None
    for hz, series in sorted(columns, key=lambda column: column[0]):
        if expect is not None and hz > expect:
            mark(expect, hz)
        if not any(math.isfinite(db) for db in series):
            mark(hz, hz + bin_hz)
        expect = hz + bin_hz
    return spans


def _revisit_s(blocks: dict[tuple[int, int, int], list[tuple[str, list[float]]]]) -> float:
    """How long a bin waits between readings, measured off rtl_power's own timestamps.

    Occupancy is a fraction of the intervals a bin was measured IN, so it only means
    something when a bin is measured often relative to how long a transmission lasts —
    and how often that is depends on how many retune hops the span needs, which the
    caller cannot work out from the response. So it is measured and reported rather than
    assumed. MEASURED on this box, 1.0 s at 1, 2, 4, 8 and 22 hops alike: rtl_power
    retunes WITHIN its `-i` interval rather than multiplying it, and the sidecar's 60 MHz
    span cap means ~25 hops is the most anyone can ask for — so across the whole allowed
    range occupancy is a fraction of one-second intervals. Reported anyway, because that
    is a fact about this hardware and this cap rather than about the arithmetic.

    The median of the gaps, so one long stall does not stand for the sweep.

    Zero means the clock could not answer — either the timestamps did not parse, or the
    revisit is faster than the one-second resolution rtl_power writes them at. Those are
    the same reading here, and neither is a rate."""
    gaps: list[float] = []
    for series in blocks.values():
        stamps: list[datetime] = []
        for when, _values in series:
            try:
                stamps.append(datetime.fromisoformat(when))
            except ValueError:
                continue
        gaps += [
            (later - earlier).total_seconds()
            for earlier, later in zip(stamps, stamps[1:], strict=False)
        ]
    return round(_percentile(gaps, 0.5), 3) if gaps else 0.0


def reduce_csv(csv_text: str, *, snr_db: float = DEFAULT_SNR_DB, channel_hz: int = 0) -> Reduced:
    """Parse rtl_power's CSV into a grid plus what is busy in it.

    Tolerant by design: rtl_power writes a partial final row when its exit timer fires
    mid-sweep, and a row with a short tail is a real measurement of fewer bins rather
    than a parse error. A row that cannot be read at all is skipped, never raised — this
    runs over a file a radio wrote, and losing one interval must not lose the sweep."""
    # Frequency block -> list of (timestamp, [dB per bin]). rtl_power emits one row per
    # BLOCK, and a wide sweep is several blocks per interval, so the blocks have to be
    # stitched by frequency rather than assumed to arrive in order.
    blocks: dict[tuple[int, int, int], list[tuple[str, list[float]]]] = {}
    for line in csv_text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            low, high, step = int(float(parts[2])), int(float(parts[3])), int(float(parts[4]))
            values = [float(p) for p in parts[6:] if p]
        except ValueError:
            continue
        if not values or step <= 0:
            continue
        blocks.setdefault((low, high, step), []).append((f"{parts[0]} {parts[1]}", values))

    if not blocks:
        return Reduced(rows=0, start_hz=0, stop_hz=0, bin_hz=0, floor_db=0.0)

    # Sorted by frequency, which is what puts `columns` in order below: rtl_power emits
    # one row per retune BLOCK, and a wide sweep's blocks do not arrive in band order.
    ordered_blocks = sorted(blocks)
    step = ordered_blocks[0][2]
    start_hz = ordered_blocks[0][0]
    stop_hz = max(high for _low, high, _s in ordered_blocks)

    # One column per bin across the whole range, one row per interval. Blocks with
    # different interval counts are trimmed to the shortest, so the grid stays square.
    intervals = min(len(v) for v in blocks.values())
    columns: list[tuple[int, list[float]]] = []
    for low, _high, block_step in ordered_blocks:
        series = blocks[(low, _high, block_step)][:intervals]
        # A block is as wide as its WIDEST row, and a short row is a missing reading for
        # the bins it stops before — not proof those bins do not exist. Trimming every
        # row to the shortest one instead deletes real bins from the whole sweep because
        # one interval was cut off, which is a normal thing for an exit timer to do: the
        # 342 kHz "hole" measured mid-band on this box is that shape and that size.
        width = max(len(vals) for _t, vals in series)
        for index in range(width):
            columns.append(
                (
                    low + index * block_step,
                    [vals[index] if index < len(vals) else math.nan for _t, vals in series],
                )
            )

    # Bins that measured SOMETHING, and their floors. A bin of non-finite values is not
    # a quiet bin: rtl_power writes those where a block handed back nothing, and letting
    # one through poisons every comparison downstream — a floor of -inf is under every
    # sample in the column, so the bin reports 100% occupancy at a peak of -inf. They
    # leave here and reappear in `uncovered`, which is what they actually are.
    busy: list[Bin] = []
    measured: list[tuple[int, list[float]]] = []
    floors: list[float] = []
    for hz, series in columns:
        finite = [db for db in series if math.isfinite(db)]
        if not finite:
            continue
        floor = _percentile(finite, FLOOR_PERCENTILE)
        measured.append((hz, finite))
        floors.append(floor)
        over = sum(1 for db in finite if db >= floor + snr_db)
        if over:
            busy.append(
                Bin(
                    hz=hz,
                    floor_db=round(floor, 1),
                    peak_db=round(max(finite), 1),
                    occupancy=round(over / len(finite), 4),
                )
            )
    busy.sort(key=lambda b: (-b.occupancy, -b.peak_db))

    # A bin whose own floor sits well above its neighbours' never went quiet.
    local = _local_floors(floors, step, channel_hz)
    steady = [
        Bin(
            hz=hz,
            floor_db=round(floor, 1),
            peak_db=round(max(series), 1),
            occupancy=1.0,
        )
        for (hz, series), floor, near in zip(measured, floors, local, strict=True)
        if floor >= near + STEADY_DB
    ]
    steady.sort(key=lambda b: -b.peak_db)

    grid = [[series[row] for _hz, series in columns] for row in range(intervals)]
    return Reduced(
        rows=intervals,
        start_hz=start_hz,
        stop_hz=stop_hz,
        bin_hz=step,
        floor_db=round(_percentile(floors, 0.5), 1),
        busy=busy,
        steady=steady,
        uncovered=_uncovered(columns, step),
        grid=grid,
        bins=len(columns),
        revisit_s=_revisit_s(blocks),
    )


def channels(reduced: Reduced, spacing_hz: int) -> list[Bin]:
    """The busy bins grouped into signals.

    A 16 kHz FM transmission in a 5 kHz sweep lights several adjacent bins, so reported
    as bins it reads as three stations a few kHz apart — which is not a thing that
    happens on a channelized band.

    Grouped by ADJACENCY rather than snapped to a grid. Snapping (`round(hz / spacing)`)
    assumes the channel grid is anchored at 0 Hz, and real band plans are not: two bins
    5 kHz apart can straddle a 15 kHz boundary and be reported as two signals, which is
    the exact failure this exists to prevent. Adjacency needs no anchor and no local
    band plan — only the knowledge that one transmission is contiguous.

    `spacing_hz` is one channel's width: bins CLOSER than that are one signal, and bins
    a full spacing apart are two channels. It is
    the caller's, because it is a band-plan fact and regionally variable: parts of the
    US use 20 kHz on 2m, and 12.5 kHz narrowband exists. Zero leaves the bins alone."""
    return _group(reduced.busy, spacing_hz)


def steady_channels(reduced: Reduced, spacing_hz: int) -> list[Bin]:
    """The steady bins grouped into signals, on the same rule as `channels`.

    A held carrier is as many bins wide as a keyed-up one, so reporting it bin by bin
    lists one repeater three times — and a list of what never went quiet is read by
    eye, where three lines that are one transmitter is the failure that matters."""
    return _group(reduced.steady, spacing_hz)


def _group(entries: list[Bin], spacing_hz: int) -> list[Bin]:
    if spacing_hz <= 0 or not entries:
        return entries
    grouped: list[Bin] = []
    cluster: list[Bin] = []
    for entry in sorted(entries, key=lambda b: b.hz):
        if cluster and entry.hz - cluster[-1].hz >= spacing_hz:
            grouped.append(_strongest(cluster))
            cluster = []
        cluster.append(entry)
    if cluster:
        grouped.append(_strongest(cluster))
    grouped.sort(key=lambda b: (-b.occupancy, -b.peak_db))
    return grouped


def _strongest(cluster: list[Bin]) -> Bin:
    """One signal, standing at its peak bin.

    A transmission's peak is at its centre and the skirts are the same signal seen
    worse, so the loudest bin names it — but occupancy is the MAXIMUM across the
    cluster, because a signal drifting a bin between intervals is present the whole
    time even though no single bin was."""
    peak = max(cluster, key=lambda b: b.peak_db)
    return Bin(
        hz=peak.hz,
        floor_db=peak.floor_db,
        peak_db=peak.peak_db,
        occupancy=max(b.occupancy for b in cluster),
    )


def waterfall_png(
    reduced: Reduced, *, floor_db: float | None = None, ceil_db: float | None = None
) -> bytes:
    """The grid as an image, scaled the way a human drags the contrast sliders.

    That gesture is not a sensitivity control — it cannot pull a signal out of noise,
    because the signal was always in the numbers. What it does is find the floor and
    stretch the palette to start just above it, which is a percentile of the data. So
    that is what this does by default, and a caller who wants the raw range can say so.
    """
    from PIL import Image  # noqa: PLC0415 - import cost only where an image is drawn

    if not reduced.grid:
        return b""
    flat = [db for row in reduced.grid for db in row if math.isfinite(db)]
    low = floor_db if floor_db is not None else _percentile(flat, 0.20)
    high = ceil_db if ceil_db is not None else _percentile(flat, 0.995)
    span = max(high - low, 1e-6)

    width, height = reduced.bins, reduced.rows
    image = Image.new("RGB", (width, height))
    pixels = []
    for row in reduced.grid:
        for db in row:
            if not math.isfinite(db):
                pixels.append((0, 0, 0))
                continue
            t = min(1.0, max(0.0, (db - low) / span))
            # Dark blue -> steel -> amber. The app's own accents rather than a rainbow:
            # a perceptually uneven palette invents edges the data does not have.
            if t < 0.5:
                u = t * 2
                pixels.append((int(14 + 40 * u), int(15 + 90 * u), int(24 + 130 * u)))
            else:
                u = (t - 0.5) * 2
                pixels.append((int(54 + 200 * u), int(105 + 100 * u), int(154 - 60 * u)))
    image.putdata(pixels)
    return _encode(image)


def _encode(image: object) -> bytes:
    import io  # noqa: PLC0415

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)  # type: ignore[attr-defined]
    return buffer.getvalue()
