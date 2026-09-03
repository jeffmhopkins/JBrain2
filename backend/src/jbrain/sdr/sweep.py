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

Pure and total: it parses text and returns numbers. No I/O, no radio, no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# How far above its own floor a bin has to sit to count as occupied for one interval.
# 8 dB rather than the 3 dB that feels generous: an 800-bin sweep over 150 intervals is
# 120,000 chances to be wrong, so a threshold that clears a couple of false positives per
# sweep is one that clears thousands per day.
DEFAULT_SNR_DB = 8.0
# Which percentile of a bin's own history counts as its noise floor.
FLOOR_PERCENTILE = 0.25
# How far a bin's own floor must sit above the sweep's before it counts as never having
# gone quiet. Generous, because the alternative to reporting these is dropping them.
STEADY_DB = 12.0


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
    """Bins sitting far above the sweep's own floor ALL the time.

    A per-bin floor makes a constant emitter invisible — it becomes its own floor, which
    is exactly right for the tuner's DC spike and its spurs, and exactly wrong for a
    repeater holding a carrier for the whole window. Occupancy statistics cannot tell
    those two apart, because they are the same measurement. So they are reported here
    rather than silently dropped, and naming which is which needs a second look at the
    channel — not more arithmetic on this sweep."""
    grid: list[list[float]] = field(default_factory=list)
    """dB per bin per interval, oldest first — what the waterfall draws."""
    bins: int = 0


def _percentile(values: list[float], fraction: float) -> float:
    """A percentile without numpy, which the api does not carry.

    Nearest-rank on a sorted copy. For a few hundred samples per bin the exactness of
    interpolation buys nothing a noise floor can tell apart."""
    if not values:
        return 0.0
    ordered = sorted(values)
    at = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[at]


def reduce_csv(csv_text: str, *, snr_db: float = DEFAULT_SNR_DB) -> Reduced:
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
        width = min(len(vals) for _t, vals in series)
        for index in range(width):
            columns.append((low + index * block_step, [vals[index] for _t, vals in series]))

    busy: list[Bin] = []
    floors: list[float] = []
    for hz, series in columns:
        floor = _percentile(series, FLOOR_PERCENTILE)
        floors.append(floor)
        over = sum(1 for db in series if db >= floor + snr_db)
        if over:
            busy.append(
                Bin(
                    hz=hz,
                    floor_db=round(floor, 1),
                    peak_db=round(max(series), 1),
                    occupancy=round(over / len(series), 4),
                )
            )
    busy.sort(key=lambda b: (-b.occupancy, -b.peak_db))

    # A bin whose own floor sits well above the sweep's is one that never went quiet.
    median_floor = _percentile(floors, 0.5)
    steady = [
        Bin(
            hz=hz,
            floor_db=round(floor, 1),
            peak_db=round(max(series), 1),
            occupancy=1.0,
        )
        for (hz, series), floor in zip(columns, floors, strict=True)
        if floor >= median_floor + STEADY_DB
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
        grid=grid,
        bins=len(columns),
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
    if spacing_hz <= 0 or not reduced.busy:
        return reduced.busy
    grouped: list[Bin] = []
    cluster: list[Bin] = []
    for entry in sorted(reduced.busy, key=lambda b: b.hz):
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
