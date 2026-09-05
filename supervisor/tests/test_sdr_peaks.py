"""What is on the air in one row: signals, not bins.

The live twin of the sweep path's `steady`, and these are the claims that make it worth
having rather than a threshold anyone could write in a line.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

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
    assert wire["peaks"] == [{"hz": 88_937_500.0, "db": -50.0, "over_db": 20.0}]


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


def test_a_carrier_wider_than_its_own_baseline_window_hides_itself() -> None:
    """Why `channel_hz` reaching the sidecar is a CORRECTNESS matter and not a
    refinement. The baseline window is 400 kHz when nobody says otherwise, and a 200 kHz
    FM carrier is half of that — so the median it is judged against is the carrier, and
    it stands 0 dB above itself. The band plan makes the window 21 channels wide, where
    one signal is under 5% of it.

    Pinned as a test because it is the failure `bands.py` already reasoned about and the
    one a retune reintroduced by dropping the raster."""
    row = _flat(400)
    # 24 bins is ~225 kHz, wider than half of a 400 kHz baseline window.
    for index in range(100, 124):
        row[index] = -50.0

    assert peaks.find(row, 88_000_000, 9375, channel_hz=0) == []
    assert len(peaks.find(row, 88_000_000, 9375, channel_hz=200_000)) == 1
