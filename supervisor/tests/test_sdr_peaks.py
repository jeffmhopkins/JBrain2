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


def _carrier(row: list[float], at: int, db: float) -> None:
    """A small peaked carrier, strongest at `at`, wide enough to BE an emission.

    A single bin is 9375 Hz. On a 200 kHz raster that is a receiver birdie, not a
    station, and `peaks.find` now says so (`MIN_WIDTH_SHARE`) — so a fixture that pokes
    one bin is testing the rejection rule rather than whatever it meant to test. Five
    bins is 47 kHz, which clears the width floor, and the taper puts the argmax where
    the caller asked for it rather than on the leftmost bin of a plateau."""
    for offset, drop in ((-2, 4.0), (-1, 1.0), (0, 0.0), (1, 1.0), (2, 4.0)):
        row[at + offset] = db - drop


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
    _carrier(row, 100, -50.0)

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)

    assert [p["hz"] for p in found] == [(88_000_000 + 100 * 9375)]


def test_the_excess_travels_with_the_signal() -> None:
    """`over_db` is what decided it was a signal at all, so it rides along rather than
    being recoverable only by someone still holding the whole row."""
    row = _flat(400)
    _carrier(row, 100, -50.0)

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
        _carrier(row, 50 + n * 60, -60.0 + n)  # each stronger than the last

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
    _carrier(row, 100, -50.0)

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
            # The shape that got it past the rejection rule, carried for `measured_hz`'s
            # reason: a signal kept or dropped on a rule nobody can check is the kind of
            # number this file exists not to produce.
            "prominence_db": 20.0,
            "width_hz": 46875.0,
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


def test_a_grid_ALREADY_ESTABLISHED_is_used_rather_than_re_derived() -> None:
    """A caller holding a session settles the grid once and passes it back.

    Deriving per row is not stable: a row whose signals happen not to agree reports raw
    measurements, the next row snaps them, and a viewer holding peaks across rows
    collects ONE station twice under two names. Measured on the FM dial: 50 held signals
    for 21 on the air, most of the surplus being exactly that.
    """
    # Two stations, too few to establish a grid on their own (MIN_RASTER_PEAKS is four),
    # so this row derives nothing and would report what it measured.
    row = _dial_at(
        bin_hz=9375,
        bins=600,
        start_hz=88_000_000,
        stations_hz=[88_310_000.0, 92_690_000.0],
    )

    alone = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)
    told = peaks.find(row, 88_000_000, 9375, channel_hz=200_000, origin=100_000.0)

    assert [signal["hz"] for signal in alone] == [
        signal["measured_hz"] for signal in alone
    ]
    assert sorted(signal["hz"] for signal in told) == [88_300_000.0, 92_700_000.0]


def test_the_grid_a_row_settles_is_the_one_a_caller_can_hold() -> None:
    """`raster_origin` is public so the session can remember what `find` would derive.

    Same input, same answer — otherwise the first row would snap to one grid and every
    row after it to another, which is the flicker this whole mechanism exists to stop.
    """
    stations = [88_300_000.0, 89_300_000.0, 92_700_000.0, 96_500_000.0, 101_300_000.0]

    settled = peaks.raster_origin(stations, 200_000)

    assert settled == 100_000.0
    # US FM sits on 88.1, 88.3, ... — half a channel off the 88.0 the band starts at.
    assert peaks._snapped(88_309_000.0, settled, 200_000) == 88_300_000.0


def test_a_channel_whose_energy_is_all_in_its_NEIGHBOURS_is_not_reported() -> None:
    """This asserted the opposite until the guard margin shipped, and the change is a
    real one rather than a fixture repair.

    Two runs 130 kHz apart both snap to the channel between them — the narrow window
    where `find` keeps them separate (wider than `SAME_SIGNAL_SHARE`) and both sit
    within `MAX_RASTER_PULL` of one channel. `_one_per_channel` was the answer:
    collapse them, keep the strongest. But look at the shape: two humps with a hole
    between them, on a channel with nothing in it. That is not one station reported
    twice; it is a channel whose energy is entirely in its neighbours, which is exactly
    what `guard_margin_db` exists to reject.

    So the collision `_one_per_channel` handles is now unreachable through `find` on any
    band with a grid. It is kept, still tested directly below, because it is still the
    right answer where there is no grid to anchor a guard on.
    """
    row = _dial_at(
        bin_hz=9375,
        bins=2000,
        start_hz=88_000_000,
        stations_hz=[106_035_000.0, 106_165_000.0],
        half_hz=20_000.0,
    )

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000, origin=100_000.0)

    assert [signal["hz"] for signal in found] == []


def test_collapsing_a_channel_keeps_the_STRONGEST_reading_of_it() -> None:
    """Not the first. "First" here means the leftmost bin, which is a carrier's shoulder
    rather than its middle — so first-wins would report every collided station a few dB
    quieter than it actually was, which is a level the agent reads."""

    def signal(hz: float, measured: float, db: float) -> dict[str, float]:
        return {"hz": hz, "measured_hz": measured, "db": db, "over_db": db + 39.0}

    collided = [
        signal(106_100_000.0, 106_060_000.0, -30.0),
        signal(106_100_000.0, 106_140_000.0, -20.0),
        signal(98_500_000.0, 98_490_000.0, -12.0),
    ]

    kept = peaks._one_per_channel(collided)

    assert [signal["db"] for signal in kept] == [-20.0, -12.0]
    assert [signal["hz"] for signal in kept] == [106_100_000.0, 98_500_000.0]


def test_the_SKIRT_of_a_loud_station_is_not_its_own_signal() -> None:
    """The owner's diagnosis, made a rule: "the peaks are sometimes sidebands".

    A skirt clears any threshold — it is the side of a loud carrier — and the raster
    snapper then gives it a clean, plausible, on-channel label. What it does not have is
    a dip on its inboard side, so it has no prominence. MEASURED on a 45 s integration
    of the owner's dial: 92.1/92.3/92.5/92.7 is one 12 dB station with three noise
    channels around it, not four stations.
    """
    row = _flat(600)
    # One carrier at 92.3 with skirts falling away either side, into 92.1 and 92.5.
    centre = int((92_300_000 - 88_000_000) / 9375)
    for offset in range(-24, 25):
        row[centre + offset] = -28.0 - abs(offset) * 0.45

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)

    assert [round(p["hz"] / 1e5) / 10 for p in found] == [92.3]


def test_shape_needs_BOTH_a_bump_and_a_width() -> None:
    """The two tests catch different families, and neither subsumes the other.

    MEASURED on a 45 s integration of the owner's dial, where 21 stations are real and
    16 are not:

      * 96.700 and 98.700 are broad flat SHELVES on the side of the two loudest
        stations — 84.4 kHz wide, which clears the width floor comfortably, with
        prominence 1.86 and 1.43 dB. Only prominence rejects them.
      * 101.900, 103.900 and 106.700 are BIRDIES 18.8 kHz wide with prominence 3.2-3.9
        dB — more prominent than the weakest real station on the band, which has 2.45.
        Only width rejects them.

    So a gate with either half missing lets a whole family through, and the two families
    are the two the owner described: "sidebands, or multiple hits very close together".
    """
    floor_hz = peaks.min_width_hz(9375, 200_000)

    # A broad flat shelf beside a loud carrier: wide, but barely a bump.
    shelf = _flat(1600)
    for offset in range(-9, 10):
        shelf[400 + offset] = -25.0 - (abs(offset) / 9.0) * 8.0
    for offset in range(10, 15):
        shelf[400 + offset] = -34.5
    for offset in range(15, 27):
        shelf[400 + offset] = -33.0
    prominence, width = peaks.shape_of(shelf, 415, 9375, 200_000)
    assert width >= floor_hz, (
        "the shelf must be wide enough that only prominence can act"
    )
    assert prominence < peaks.MIN_PROMINENCE_DB
    assert not peaks._has_shape(shelf, 415, 9375, 200_000, floor_hz)

    # A birdie: a clean bump, and far too narrow to be an emission on this raster.
    birdie = _flat(1600)
    birdie[400] = -34.0
    birdie[401] = -34.5
    prominence, width = peaks.shape_of(birdie, 400, 9375, 200_000)
    assert prominence >= peaks.MIN_PROMINENCE_DB, (
        "must be a real bump, so only width acts"
    )
    assert width < floor_hz
    assert not peaks._has_shape(birdie, 400, 9375, 200_000, floor_hz)


def test_a_BIRDIE_two_bins_wide_is_not_a_station() -> None:
    """The other family in the surplus: 19-28 kHz spikes with row-to-row variation of
    0.25 dB where the band median is 0.29. Something that narrow and that steady is the
    receiver, not a broadcast emission — a station on a 200 kHz raster is 180 kHz wide.

    Prominence alone does NOT catch these: measured on the owner's dial the birdies run
    2.6-3.9 dB of prominence while the weakest real station has 2.45. Width is what
    separates them, and it is why both tests are here.
    """
    row = _flat(1600)
    at = int((101_900_000 - 88_000_000) / 9375)
    row[at] = -34.0
    row[at + 1] = -34.5

    assert peaks.find(row, 88_000_000, 9375, channel_hz=200_000) == []


def test_a_real_carrier_clears_both_shape_tests() -> None:
    """The other side of the same rule — it must not be so strict that it deletes the
    band. A 180 kHz FM carrier is 19 bins wide and stands well clear of the noise."""
    row = _flat(1600)
    centre = int((96_500_000 - 88_000_000) / 9375)
    for offset in range(-10, 11):
        row[centre + offset] = -25.0 - (offset / 10.0) ** 2 * 6.0

    found = peaks.find(row, 88_000_000, 9375, channel_hz=200_000)

    assert len(found) == 1
    assert found[0]["prominence_db"] >= peaks.MIN_PROMINENCE_DB
    assert found[0]["width_hz"] >= peaks.min_width_hz(9375, 200_000)


def test_width_is_not_judged_without_a_BAND_PLAN_to_judge_it_against() -> None:
    """Prominence is a question about shape and needs no plan. Width is a question about
    bandwidth and cannot be asked without one — with no raster a signal may legitimately
    be one bin, and a floor guessed from the transform's own resolution would delete it.
    """
    row = _flat(400)
    row[100] = -50.0

    assert len(peaks.find(row, 88_000_000, 9375, channel_hz=0)) == 1
    assert peaks.find(row, 88_000_000, 9375, channel_hz=200_000) == []


def test_the_guard_margin_separates_the_real_dial_but_is_not_wired_in() -> None:
    """The measurement that decided NOT to ship it, kept so the next attempt starts from
    numbers rather than from scratch.

    On a 45 s integration of the owner's dial it is perfect — 21 of 21 real stations
    kept and 16 of 16 surplus rejected, beating prominence+width, which leaves one. But
    the worst real station clears by 1.06 dB against a best artefact of 0.33, and on a
    short window that 0.73 dB of daylight closes and inverts. The decision row is a mean
    of `HOLD_SWEEPS` sweeps, about 1.8 s. So the function exists, is correct, and
    `_has_shape` does not call it.
    """
    # A carrier with clean troughs either side: the shape a real station has.
    row = _flat(1600)
    at = int((96_500_000 - 88_000_000) / 9375)
    for offset in range(-9, 10):
        row[at + offset] = -25.0 - (abs(offset) / 9.0) * 10.0

    assert peaks.guard_margin_db(row, at, 9375, 200_000) >= peaks.MIN_GUARD_MARGIN_DB
    # ...and it is not consulted: a shape that passes prominence and width is kept even
    # where the guard margin would reject it.
    assert peaks._has_shape(row, at, 9375, 200_000, peaks.min_width_hz(9375, 200_000))


def test_the_guard_margin_is_not_ASKED_where_a_channel_is_a_few_bins_wide() -> None:
    """Its guards sit half a channel out and are half a channel wide, so on a plan whose
    channels are three bins across they land inside the neighbours' skirts and measure
    nothing. NaN says "not asked", which a caller must not read as "failed" — and it is
    what keeps this away from 2 m repeater pairs and 25 kHz airband, where two adjacent
    occupied channels is the design rather than a defect."""
    row = _flat(400)
    row[200] = -30.0

    assert math.isnan(peaks.guard_margin_db(row, 200, 9375, 25_000))
    assert math.isnan(peaks.guard_margin_db(row, 200, 9375, 0))
    assert not math.isnan(peaks.guard_margin_db(row, 200, 9375, 200_000))
