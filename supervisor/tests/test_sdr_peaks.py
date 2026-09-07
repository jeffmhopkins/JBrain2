"""What is on the air in one row: signals, not bins.

The live twin of the sweep path's `steady`, and these are the claims that make it worth
having rather than a threshold anyone could write in a line.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load():
    # Same loader as test_sdr_iq.py: `deploy/sdr/` is not an installed package, so it is
    # loaded by path, and the directory goes on sys.path so one convention covers every
    # sidecar module.
    sdr_dir = str(DEPLOY / "sdr")
    if sdr_dir not in sys.path:
        sys.path.insert(0, sdr_dir)
    spec = importlib.util.spec_from_file_location("sdr_peaks", DEPLOY / "sdr/peaks.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_peaks"] = module
    spec.loader.exec_module(module)
    return module


peaks = _load()


def _load_listen():
    """`listen.py` too, for the claim that is about the FRAME rather than the rule."""
    spec = importlib.util.spec_from_file_location(
        "sdr_listen_pk", DEPLOY / "sdr/listen.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_listen_pk"] = module
    spec.loader.exec_module(module)
    return module


def _flat(count: int, level: float = -70.0) -> list[float]:
    return [level] * count


def test_a_carrier_and_its_skirt_are_one_signal() -> None:
    """A 200 kHz FM transmission in a 9375 Hz row lights twenty adjacent bins. Reported
    as bins it reads as twenty stations a few kHz apart, which is not a thing that
    happens on a channelised band."""
    row = _flat(400)
    for index in range(100, 112):
        row[index] = -50.0 - abs(index - 105)

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)

    assert len(found) == 1
    # The strongest bin of the cluster, not its first or its middle by position.
    assert found[0]["hz"] == (88_000_000 + 105 * 9375)


def test_a_tilted_floor_is_not_a_band_full_of_signals() -> None:
    """The whole argument for judging a bin against its NEIGHBOURS. Band-edge rolloff
    and per-hop noise mean the floor is not one number across a row — measured on the
    box, two hops of one 144-148 sweep sat 0.68 dB apart. Against a span-wide median the
    high end of a tilt reads as 'above the floor' and the list is the whole band."""
    row = [-90.0 + index * 0.1 for index in range(400)]  # a 40 dB tilt, no signal in it

    assert peaks.find(row, 144_000_000, 9375, channel_hz=15_000) == []


def test_a_bin_that_measured_nothing_is_skipped_not_floored() -> None:
    """A hop that lost a block leaves NaN, which is not a quiet bin. Letting one in
    poisons every comparison around it — the same choice the sweep path makes."""
    row = _flat(400)
    row[200] = math.nan
    row[100] = -50.0

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)

    assert [p["hz"] for p in found] == [(88_000_000 + 100 * 9375)]


def test_the_excess_travels_with_the_signal() -> None:
    """`over_db` is what decided it was a signal at all, so it rides along rather than
    being recoverable only by someone still holding the whole row."""
    row = _flat(400)
    row[100] = -50.0

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)

    assert found[0]["over_db"] == 20.0
    assert found[0]["db"] == -50.0


def test_quiet_air_reports_nothing_rather_than_its_loudest_noise() -> None:
    """A band with nothing on it must produce an empty list, not a ranked list of noise.
    The threshold is what makes the view worth looking at."""
    row = [-70.0 + (index % 3) * 0.5 for index in range(400)]

    assert peaks.find(row, 144_000_000, 9375, channel_hz=15_000) == []


def test_the_strongest_come_first_and_the_list_is_capped() -> None:
    """A row with more signals than the cap is a band that wants looking at rather than
    a list that wants reading — and the cap bounds every viewer's frame."""
    row = _flat(4000)
    for n in range(peaks.MAX_PEAKS + 10):
        row[50 + n * 60] = -60.0 + n  # each stronger than the last

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)

    assert len(found) == peaks.MAX_PEAKS
    assert [p["db"] for p in found] == sorted((p["db"] for p in found), reverse=True)


def test_without_a_channel_width_only_touching_bins_are_one_signal() -> None:
    """Zero means the caller did not say. Two carriers a few bins apart are then two
    signals, which is the honest answer without a band plan to say otherwise."""
    row = _flat(400)
    row[100] = -50.0
    row[104] = -50.0

    assert len(peaks.find(row, 88_000_000, 9375, channel_hz=0)) == 2


def test_the_frame_carries_what_it_found() -> None:
    """On the frame rather than computed per viewer, because it is a measurement: the
    agent's tools and the picture must not be able to disagree about what was on the
    air, and only one of them is looking at the row."""
    listen = _load_listen()
    row = _flat(400)
    row[100] = -50.0

    frame = listen.Frame(
        at=1.0,
        start_hz=88_000_000,
        bin_hz=9375,
        db=row,
        peaks=peaks.find(row, 88_000_000, 9375, channel_hz=200_000),
    )

    wire = frame.as_dict()
    # `measured_hz` alongside `hz`, and here they are EQUAL: one signal cannot establish
    # where a channel grid sits, so `_raster_origin` abstains and nothing is snapped.
    assert wire["peaks"] == [
        {
            "hz": 88_937_500.0,
            "measured_hz": 88_937_500.0,
            "db": -50.0,
            "over_db": 20.0,
        }
    ]


def test_a_quiet_row_carries_an_empty_list_not_a_missing_key() -> None:
    """Empty is a real answer — a quiet band — and a viewer that had to tell "no peaks"
    from "this build does not report peaks" would have to guess."""
    listen = _load_listen()

    frame = listen.Frame(at=1.0, start_hz=88_000_000, bin_hz=9375, db=_flat(50))

    assert frame.as_dict()["peaks"] == []


def test_a_dip_inside_a_carrier_does_not_split_it_into_two_stations() -> None:
    """REPORTED by the owner: "sometimes one signal will produce 2-3 overlapping peak
    detections". A real carrier is contiguous, but noise drops the odd bin inside it
    back under the threshold, and a rule that split on one bin reported one station
    twice."""
    row = _flat(400)
    for index in range(100, 112):
        row[index] = -50.0
    row[105] = -70.0  # one bin of the carrier dips into the noise

    found = peaks.find(row, 88_000_000, 9375, channel_hz=0)

    assert len(found) == 1


def test_the_band_plan_widens_the_fold_and_cannot_narrow_it() -> None:
    """`channel_hz` zero means the caller did not say — which is what a RETUNE used to
    leave the sidecar with, and it is not a licence to split every carrier."""
    assert peaks.MIN_FOLD_BINS >= 3
    row = _flat(400)
    for index in range(100, 110):
        row[index] = -50.0
    row[104] = -70.0  # a bin of the carrier back in the noise

    assert len(peaks.find(row, 88_000_000, 9375, channel_hz=200_000)) == 1
    assert len(peaks.find(row, 88_000_000, 9375, channel_hz=0)) == 1


def test_a_carrier_is_found_even_when_it_fills_much_of_its_own_baseline() -> None:
    """This test used to assert the OPPOSITE, and pinning it kept a real fault alive.

    It said a 200 kHz carrier in a 400 kHz baseline window "hides itself", stands 0 dB
    above itself, and that naming `channel_hz` is what rescues it. The first half was
    true; the second was a workaround. `channel_hz` only ever made the window WIDER, so
    on any row narrower than the window — a sweep under 400 kHz, a 256 kS/s capture, a
    200 kHz raster — the baseline silently became a global median and the signal was
    still measured against itself. Measured on air 2026-09-06: a 162.3-162.7 sweep
    missed NOAA on 162.550 while it was the strongest bin in the row, 9.8 dB over its
    own median.

    Two changes make the failure unreachable rather than avoidable: the window is capped
    at a third of the row, and the statistic is the 35th percentile rather than the
    median, so a signal filling a third of its own reference still has noise under it.
    Naming the raster now places the window better; it is no longer a rescue."""
    row = _flat(400)
    # 24 bins is ~225 kHz — more than half a 400 kHz baseline window, and the exact
    # case that used to read as nothing at all.
    for index in range(100, 124):
        row[index] = -50.0

    assert len(peaks.find(row, 88_000_000, 9375, channel_hz=0)) == 1
    assert len(peaks.find(row, 88_000_000, 9375, channel_hz=200_000)) == 1


def test_the_baseline_window_never_exceeds_a_third_of_the_row() -> None:
    """The clamp, as the invariant rather than one of its consequences.

    Without it the window grows on demand and the row does not: 21 channels of a 200 kHz
    raster is 4.2 MHz, which is wider than any row this radio can produce."""
    for bins, bin_hz, channel_hz in (
        (128, 3125.0, 25_000),  # the 162.3-162.7 sweep that missed NOAA
        (256, 1000.0, 0),  # a 256 kS/s capture
        (4000, 600.0, 200_000),  # the FM dial
        (4000, 600.0, 0),
    ):
        assert peaks.baseline_width(bins, bin_hz, channel_hz) <= max(
            peaks.BASELINE_MIN_BINS, bins // 3
        )


def _dial(bin_hz: float, bins: int, stations: list[tuple[float, float]]) -> list[float]:
    """A noise floor with broadcast-shaped humps on it. Each station is (offset_hz,
    width_hz) from bin 0 and stands 40 dB over the floor with a ragged top, because a
    flat one would let a rule pass by finding a plateau's single argmax."""
    db = [-90.0 + (i % 5) * 0.4 for i in range(bins)]
    for offset_hz, width_hz in stations:
        centre = int(offset_hz / bin_hz)
        half = int(width_hz / bin_hz / 2)
        for i in range(max(0, centre - half), min(bins, centre + half + 1)):
            db[i] = -50.0 + ((i * 7) % 11) * 0.5
    return db


def test_two_stations_one_RASTER_apart_are_two_signals() -> None:
    """B4. The fold rule was a GAP — "a gap up to a whole channel wide is still one
    signal" — and two stations exactly one raster apart always have a clear gap narrower
    than the raster between their skirts. MEASURED: two FM stations 200 kHz apart came
    back as ONE signal, which is the failure mode where a band looks emptier than it is.

    A minimum peak-to-peak DISTANCE is the right shape, and 0.6 of the raster is the
    number `sdrPeaks.ts` already holds signals together with."""
    bin_hz = 9_375.0
    db = _dial(bin_hz, 512, [(1_200_000.0, 180_000.0), (1_400_000.0, 180_000.0)])

    found = peaks.find(db, 95_000_000, bin_hz, channel_hz=200_000)

    assert len(found) == 2, found
    apart = abs(found[0]["hz"] - found[1]["hz"])
    assert apart == pytest.approx(200_000, abs=2 * bin_hz)


def test_one_broadcast_carrier_is_still_ONE_signal() -> None:
    """The other half, and the reason the gap rule existed: a 180 kHz carrier is twenty
    bins wide with a ragged top, and a rule that reported each local maximum would put
    four pills on one station — which the owner saw and reported."""
    bin_hz = 9_375.0
    db = _dial(bin_hz, 512, [(1_200_000.0, 180_000.0)])

    found = peaks.find(db, 95_000_000, bin_hz, channel_hz=200_000)

    assert len(found) == 1, found


def test_a_carrier_split_by_a_NOTCH_is_not_two_stations() -> None:
    """Adjacency alone would split here, and the distance rule is what puts it back: two
    fragments 20 kHz apart are far inside one 200 kHz channel."""
    bin_hz = 9_375.0
    db = _dial(bin_hz, 512, [(1_200_000.0, 180_000.0)])
    notch = int(1_200_000.0 / bin_hz)
    for i in (notch - 1, notch, notch + 1):
        db[i] = -90.0

    found = peaks.find(db, 95_000_000, bin_hz, channel_hz=200_000)

    assert len(found) == 1, found


def test_with_no_band_plan_only_adjacency_decides() -> None:
    """`channel_hz` zero means the caller did not say — a hand-typed range, a survey of
    an unplanned band — and inventing a spacing there would be a measurement made up."""
    bin_hz = 9_375.0
    db = _dial(bin_hz, 512, [(1_200_000.0, 180_000.0), (1_400_000.0, 180_000.0)])

    found = peaks.find(db, 95_000_000, bin_hz)

    assert len(found) == 2, found


def _dial_at(
    bin_hz: float,
    bins: int,
    start_hz: float,
    stations_hz: Sequence[float],
    half_hz: float = 60_000.0,
):
    """A row with a real hump at each named frequency — highest at the centre.

    A ROUNDED top, not the flat ragged one the counting tests use: these tests ask WHERE
    a signal was reported, so the fixture has to have an unambiguous answer. The ripple
    stays, small enough not to move the peak but large enough that nothing here
    passes by being perfectly smooth."""
    db = [-90.0 + (i % 5) * 0.4 for i in range(bins)]
    half = max(1.0, half_hz / bin_hz)
    for hz in stations_hz:
        centre = (hz - start_hz) / bin_hz
        for i in range(bins):
            away = abs(i - centre) / half
            if away <= 1.0:
                # Loudest at the CENTRE, falling 8 dB to the skirt. Louder wins
                # where two overlap, which is what a receiver sees.
                level = -42.0 - 8.0 * away * away + ((i * 7) % 11) * 0.05
                db[i] = max(db[i], level)
    return db


def test_a_station_is_reported_as_its_CHANNEL_not_as_where_the_snapshot_peaked() -> (
    None
):
    """A hopped row samples each slice for a fraction of a millisecond, and a
    wideband-FM carrier sweeps its own ±75 kHz of deviation the whole time — so the
    argmax of one snapshot is where the modulation happened to be.

    MEASURED ON THE DIAL: peaks landing -25.0, +15.6, +15.6 and +3.1 kHz off their
    channels, and `88.263` and `88.356` BOTH reported for 88.3 — one station, two pills,
    93 kHz apart. On a band with a raster a signal is a channel, so that is what it is
    called; `measured_hz` keeps what was actually seen."""
    start, bin_hz = 88_000_000.0, 9_375.0
    # Real US FM channels — 88.1 + n*200 kHz — each nudged by a plausible snapshot
    # error.
    truth = [88_100_000, 88_700_000, 89_300_000, 90_100_000, 90_700_000]
    wobble = [-15_600, 15_600, -21_900, 3_100, 18_700]
    db = _dial_at(
        bin_hz, 2332, start, [t + w for t, w in zip(truth, wobble, strict=True)]
    )

    found = peaks.find(db, start, bin_hz, channel_hz=200_000)

    assert len(found) == len(truth)
    for entry, want in zip(sorted(found, key=lambda e: e["hz"]), truth, strict=True):
        # Within a bin of the real channel. The grid ORIGIN is estimated from peaks that
        # sit on bin centres, so it cannot be located finer than about one bin —
        # 9.4 kHz here, against the ±60 kHz scatter it replaces.
        assert abs(entry["hz"] - want) <= bin_hz, (entry, want)
        # The measurement is still there to check the claim against.
        assert abs(entry["measured_hz"] - entry["hz"]) < 100_000, entry
    assert any(e["measured_hz"] != e["hz"] for e in found)


def test_the_same_station_gets_the_same_LABEL_row_after_row() -> None:
    """The bug, stated as a property. One station reported at 88.263 on one row and
    88.356 on the next is two pills 93 kHz apart, and the owner's dial filled with 66 of
    them for a band that has about 25 stations.

    Snapping does not merely reduce the scatter — it removes it. Two rows that saw the
    same dial through different snapshots must produce the SAME labels, or the pills
    still multiply."""
    start, bin_hz = 88_000_000.0, 9_375.0
    truth = [88_100_000, 88_700_000, 89_300_000, 90_100_000, 90_700_000]
    first = [-15_600, 15_600, -21_900, 3_100, 18_700]
    second = [18_700, -21_900, 15_600, -15_600, 3_100]

    rows = [
        peaks.find(
            _dial_at(
                bin_hz, 2332, start, [t + w for t, w in zip(truth, wob, strict=True)]
            ),
            start,
            bin_hz,
            channel_hz=200_000,
        )
        for wob in (first, second)
    ]

    labels = [sorted(e["hz"] for e in row) for row in rows]
    assert labels[0] == labels[1], labels
    # ...and they really were different measurements, so this is not passing by the two
    # rows being identical.
    assert sorted(e["measured_hz"] for e in rows[0]) != sorted(
        e["measured_hz"] for e in rows[1]
    )


def test_scatter_too_wide_to_BE_a_grid_is_not_called_one() -> None:
    """The abstain that couples this to the dwell, and it is the honest half of the fix.

    With `HOP_SEGMENTS = 4` each hop saw 0.43 ms and peaks landed up to ±60 kHz off
    their channels — a third of a 200 kHz raster, which is not a grid anyone can point
    at. The threshold refuses it rather than rounding noise onto a band plan. What makes
    the snap work is the INTEGRATION (64 segments, 6.8 ms), which is why the two shipped
    together: this rule is what says so out loud if the dwell is ever cut back."""
    start, bin_hz = 88_000_000.0, 9_375.0
    truth = [88_100_000, 88_700_000, 89_300_000, 90_100_000, 90_700_000]
    as_it_was = [-25_000, 15_600, -46_000, 3_100, 56_000]
    db = _dial_at(
        bin_hz, 2332, start, [t + w for t, w in zip(truth, as_it_was, strict=True)]
    )

    found = peaks.find(db, start, bin_hz, channel_hz=200_000)

    assert len(found) == len(truth)
    for entry in found:
        assert entry["hz"] == entry["measured_hz"], entry


def test_the_grid_ORIGIN_is_measured_and_not_assumed() -> None:
    """The spacing is in the band plan; the origin is not. US FM is 200 kHz spaced and
    sits half a channel off the 88.0 its band starts at, while airband is 25 kHz spaced
    and sits ON 118.000. Assuming either rule labels the other band wrong by half a
    channel — so the phase is taken from the signals themselves."""
    bin_hz = 9_375.0
    # A band whose stations sit ON the band edge's multiples, not half a channel in.
    start = 118_000_000.0
    truth = [118_050_000, 118_150_000, 118_300_000, 118_450_000, 118_600_000]
    scatter = [4_000, -5_000, 4_500, -3_000, 5_000]
    db = _dial_at(
        bin_hz,
        800,
        start,
        [t + w for t, w in zip(truth, scatter, strict=True)],
        half_hz=15_000,
    )

    found = peaks.find(db, start, bin_hz, channel_hz=50_000)

    # EXACTLY the channels, which is the discriminating part: the grid here sits ON the
    # multiples of 50 kHz, and a rule that assumed FM's half-channel offset would put
    # every one of them 25 kHz out. The measured phase is what tells them apart.
    assert sorted(e["hz"] for e in found) == truth
    # ...and the peaks really were off-channel, so this is not passing by them landing
    # on the right answer unaided. `any`, not `all`: a peak whose bin happens to centre
    # on its own channel is a legitimate outcome, not a failure to snap.
    assert any(e["measured_hz"] != e["hz"] for e in found)


def test_a_band_with_NO_grid_is_left_where_it_was_found() -> None:
    """The abstain, and the reason this is a measurement rather than a formula: signals
    that do not agree on a phase must not be snapped onto one. Rounding them would
    invent a band plan and report it as fact."""
    start, bin_hz = 400_000_000.0, 9_375.0
    # Deliberately off any 200 kHz grid, and scattered in phase.
    scattered = [400_137_000, 400_611_000, 401_044_000, 401_723_000, 402_069_000]
    db = _dial_at(bin_hz, 800, start, scattered)

    found = peaks.find(db, start, bin_hz, channel_hz=200_000)

    assert found, "the signals are still found"
    for entry in found:
        assert entry["hz"] == entry["measured_hz"], entry


def test_too_few_signals_cannot_establish_a_grid() -> None:
    """Three peaks can agree on a spacing by coincidence; a dial-full cannot. Below
    `MIN_RASTER_PEAKS` nothing is snapped, however tempting the arithmetic looks."""
    start, bin_hz = 88_000_000.0, 9_375.0
    db = _dial_at(bin_hz, 800, start, [88_120_000, 88_520_000])

    found = peaks.find(db, start, bin_hz, channel_hz=200_000)

    assert len(found) == 2
    for entry in found:
        assert entry["hz"] == entry["measured_hz"]


def test_a_signal_far_from_every_channel_keeps_its_own_frequency() -> None:
    """A grid the row agrees on does not make every signal a member of it: a pirate or a
    spur sitting between two channels is nearer neither, and calling it one would
    move it by up to half a raster."""
    start, bin_hz = 88_000_000.0, 9_375.0
    on_grid = [88_100_000, 88_700_000, 89_300_000, 90_100_000]
    stray = 90_800_000.0  # 100 kHz off — exactly between two channels
    db = _dial_at(bin_hz, 2332, start, [*on_grid, stray])

    found = peaks.find(db, start, bin_hz, channel_hz=200_000)
    by_measured = {e["measured_hz"]: e for e in found}
    odd = min(by_measured, key=lambda hz: abs(hz - stray))

    assert abs(odd - stray) < bin_hz
    assert by_measured[odd]["hz"] == odd, "left where it was found"
