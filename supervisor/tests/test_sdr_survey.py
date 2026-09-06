"""A survey as an accumulator over the spectrum, with no radio and no subprocess.

`SurveyRows` is what replaced `rtl_power -e`: a survey was never a different way of
MEASURING, it is the same spectrum integrated for longer and written down instead of
drawn (`docs/plans/SDR_RECEIVER_CONVERGENCE_PLAN.md` A5/B2). What can go wrong here is
arithmetic and text, both of which a synthetic frame can provoke exactly — and the text
has a reader with its own opinions (`backend/src/jbrain/sdr/sweep.py`), so the shape is
asserted against what that reader parses rather than against what looks right.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load():
    sdr_dir = str(DEPLOY / "sdr")
    if sdr_dir not in sys.path:
        sys.path.insert(0, sdr_dir)
    spec = importlib.util.spec_from_file_location(
        "sdr_listen_survey", DEPLOY / "sdr/listen.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sdr_listen_survey"] = module
    spec.loader.exec_module(module)
    return module


listen = _load()


def _frame(
    db: list[float],
    at: float = 0.0,
    start_hz: int = 144_000_000,
    bin_hz: float = 1_000.0,
):
    return listen.Frame(at=at, start_hz=start_hz, bin_hz=bin_hz, db=db)


def _fields(line: str) -> list[str]:
    return [p.strip() for p in line.split(", ")]


def test_a_row_carries_the_six_fields_its_reader_parses() -> None:
    """date, time, low, high, step, samples, then one dB figure per bin. Asserted by
    POSITION because that is how `sweep.reduce_csv` reads it — it takes parts[2:5] as
    the range and parts[6:] as the numbers, and a field inserted anywhere before those
    would shift a whole survey silently."""
    rows = listen.SurveyRows(1_000)

    out = rows.push(_frame([-70.0, -60.0], at=0.0)) + rows.flush()

    assert len(out) == 1
    parts = _fields(out[0])
    assert len(parts) == 8
    assert parts[2] == "144000000"
    assert parts[3] == str(144_000_000 + 2 * 1_000)
    assert parts[4] == "1000.00"
    assert parts[5] == "1"
    assert [float(p) for p in parts[6:]] == [-70.0, -60.0]


def test_an_interval_closes_on_the_clock_the_frames_carry() -> None:
    """The frames' own timestamps, not a clock of this object's: a survey reduced from
    rows that claim a second each has to have actually integrated a second of them."""
    rows = listen.SurveyRows(1_000, interval_s=1.0)

    assert rows.push(_frame([-70.0], at=100.0)) == []
    assert rows.push(_frame([-70.0], at=100.5)) == []
    out = rows.push(_frame([-70.0], at=101.0))

    assert len(out) == 1
    assert _fields(out[0])[5] == "3"  # all three, then the interval closed


def test_frames_are_averaged_in_power_and_not_in_decibels() -> None:
    """A mean of dB values is a GEOMETRIC mean of powers, which sits below the
    arithmetic one — and `reduce_csv` takes a percentile of these numbers and calls the
    result a noise floor, so the error would land exactly where it is read as fact.

    -70 and -60 dB average to -63.6 dB in power and to -65 in decibels: 1.4 dB, which is
    most of the 6 dB `STEADY_DB` margin the classifier works with."""
    rows = listen.SurveyRows(1_000, interval_s=10.0)
    rows.push(_frame([-70.0], at=0.0))
    rows.push(_frame([-60.0], at=0.1))

    got = float(_fields(rows.flush()[0])[6])

    assert got == pytest.approx(10 * math.log10((1e-7 + 1e-6) / 2), abs=0.01)
    assert got > -65.0  # ...which is what the wrong answer would have been


def test_a_finer_capture_is_folded_to_the_width_that_was_asked_for() -> None:
    """A survey asks for a width; the capture has whatever `rate / bins` gives. Folding
    adjacent bins is a real average of real measurements, so the row can honestly claim
    the coarser width."""
    rows = listen.SurveyRows(4_000)

    eight = [-70.0, -70.0, -70.0, -70.0, -60.0, -60.0, -60.0, -60.0]
    out = rows.push(_frame(eight)) + rows.flush()

    parts = _fields(out[0])
    assert parts[4] == "4000.00"
    assert len(parts) == 8  # two bins, not eight
    assert float(parts[6]) == pytest.approx(-70.0, abs=0.01)


def test_a_coarser_capture_is_not_given_resolution_it_never_had() -> None:
    """The opposite case, and the one where inventing would be easy: asked for 100 Hz
    bins off a 1 kHz capture, the row states 1 kHz. `reduce_csv` reads the width off the
    LINE, so a row that lied here would place every bin at the wrong frequency."""
    rows = listen.SurveyRows(100)

    out = rows.push(_frame([-70.0, -60.0])) + rows.flush()

    assert _fields(out[0])[4] == "1000.00"


def test_a_retune_ends_the_interval_rather_than_being_averaged_across() -> None:
    """Two bands is two measurements. Averaging across a retune would report a band that
    was never on the air, at a frequency neither row was measured at."""
    rows = listen.SurveyRows(1_000, interval_s=100.0)
    rows.push(_frame([-70.0], at=0.0))

    out = rows.push(_frame([-50.0], at=0.1, start_hz=440_000_000))

    assert len(out) == 1
    assert _fields(out[0])[2] == "144000000"
    assert float(_fields(out[0])[6]) == pytest.approx(-70.0, abs=0.01)
    # ...and the new band is open, not lost.
    assert _fields(rows.flush()[0])[2] == "440000000"


def test_a_row_that_ends_short_is_still_written_down() -> None:
    """A survey stops on a deadline, not on an interval boundary. Dropping the open
    interval would silently shorten every survey by up to a second — and on a
    fifteen-second probe that is a fifteenth of the measurement."""
    rows = listen.SurveyRows(1_000, interval_s=60.0)
    rows.push(_frame([-70.0], at=0.0))

    assert len(rows.flush()) == 1
    assert rows.flush() == []  # ...and only once


def test_a_silent_bin_is_written_as_a_number_and_not_as_minus_infinity() -> None:
    """`-inf` is what `log10(0)` gives and what no CSV reader survives. The floor is
    `iq.DB_FLOOR`, the same one the live picture uses, so the two say the same thing
    about the same silence."""
    rows = listen.SurveyRows(1_000)

    out = rows.push(_frame([listen.iq.DB_FLOOR - 50.0])) + rows.flush()

    assert float(_fields(out[0])[6]) == pytest.approx(listen.iq.DB_FLOOR, abs=0.01)


def test_an_empty_or_nonsense_frame_costs_nothing() -> None:
    """This runs over rows a radio produced while it was still producing them."""
    rows = listen.SurveyRows(1_000)

    assert rows.push(_frame([])) == []
    assert rows.push(_frame([-70.0], bin_hz=0)) == []
    assert rows.flush() == []
    assert rows.rows == 0


# --- and the reader on the other side ------------------------------------------------


def _reducer():
    """`backend/src/jbrain/sdr/sweep.py`, loaded BY PATH.

    The producer and its reader live in different packages and different containers, and
    the only thing joining them is a line of text. A test that asserted the shape from
    one side would be asserting what this file believes; this asks the code that will
    actually read it. Pure and total by its own docstring — no I/O, no radio, no clock —
    which is what makes loading it here possible at all."""
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "jbrain_sweep_reduce", root / "backend/src/jbrain/sdr/sweep.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE it is executed: `@dataclass` looks its own module up in
    # `sys.modules` to resolve annotations, and a module missing from there fails inside
    # the decorator with an error that says nothing about the loader.
    sys.modules["jbrain_sweep_reduce"] = module
    spec.loader.exec_module(module)
    return module


def _survey(loud_from: float, loud_until: float, seconds: int = 20) -> str:
    """A quiet band with one carrier in bin 7, on air between those two times."""
    rows = listen.SurveyRows(25_000, interval_s=1.0)
    lines: list[str] = []
    for tick in range(seconds * 4):
        at = tick * 0.25
        db = [-90.0] * 16
        if loud_from <= at < loud_until:
            db[7] = -70.0
        lines.extend(rows.push(_frame(db, at=at, bin_hz=25_000.0)))
    lines.extend(rows.flush())
    return "\n".join(lines)


def test_the_reader_on_the_other_side_gets_back_what_was_measured() -> None:
    """The whole contract of B2 in one assertion: the survey stopped being `rtl_power`,
    and the api that reduces its CSV was not told, because there was nothing to tell.

    A carrier on air for HALF the window, which is the case the reduction is built
    around — occupancy is a percentage of the window rather than a peak in dB, because a
    one-off burst and a channel busy half the hour have the same peak."""
    out = _reducer().reduce_csv(_survey(0.0, 10.0), snr_db=8.0)

    assert out.rows == 16  # a 20 s window closes 16 one-second intervals of five frames
    assert out.start_hz == 144_000_000
    assert out.bin_hz == 25_000
    assert out.stop_hz == 144_000_000 + 16 * 25_000
    assert out.floor_db == pytest.approx(-90.0, abs=0.5)

    busy = [b for b in out.busy if b.occupancy > 0.2]
    assert len(busy) == 1
    assert busy[0].hz == pytest.approx(144_000_000 + 7 * 25_000, abs=25_000)
    assert busy[0].occupancy == pytest.approx(0.5, abs=0.15)


def test_a_carrier_that_never_goes_quiet_reads_as_steady_and_not_as_idle() -> None:
    """The reduction's own trap, reached through this producer: a bin up the WHOLE
    window becomes its own floor and reads as 0% occupied, so it is found by comparison
    with its neighbours instead. A survey that reported the strongest thing on the band
    as idle would be the same class of wrong this plan keeps finding."""
    out = _reducer().reduce_csv(_survey(0.0, 1e9), snr_db=8.0)

    assert not [b for b in out.busy if b.occupancy > 0.2]
    carrier = pytest.approx(144_000_000 + 7 * 25_000, abs=25_000)
    assert [b.hz for b in out.steady] == [carrier]
