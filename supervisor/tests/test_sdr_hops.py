"""The hop plan is written twice, in two containers, and must not drift (C28).

`backend/src/jbrain/sdr/bands.py` PLANS a hopped row — how many hops, where each tunes,
how wide the trusted middle of each is — and `deploy/sdr/listen.py` STITCHES what comes
back. They cannot import each other: the sidecar ships in its own apt-only image and
holds no backend code. So the arithmetic is a copy, and a copy that drifts produces a
stitched row whose bin-to-hertz mapping is wrong with nothing anywhere to say so — the
axis lies and the picture looks fine.

This is the only place the two are in one process. It is a supervisor test because
supervisor's pytest is what already loads `deploy/sdr/` by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SDR = _ROOT / "deploy/sdr"


def _by_path(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `@dataclass` resolves annotations through `sys.modules`,
    # and a module missing from it raises during class creation rather than on use.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bands() -> Any:
    if str(_SDR) not in sys.path:
        sys.path.insert(0, str(_SDR))
    return _by_path("jbrain_bands", _ROOT / "backend/src/jbrain/sdr/bands.py")


@pytest.fixture(scope="module")
def listen() -> Any:
    if str(_SDR) not in sys.path:
        sys.path.insert(0, str(_SDR))
    return _by_path("sdr_listen_for_hops", _SDR / "listen.py")


def test_the_two_containers_agree_on_what_a_capture_is_worth(bands, listen) -> None:
    """The constant itself. Spelled `5 // 6` inside an expression on the sidecar until
    C28, which is a copy a reader cannot see is a copy."""
    assert listen.TRUSTED_FILL == bands.TRUSTED_FILL


def test_the_planner_and_the_stitcher_agree_on_every_rung_of_the_ladder(
    bands, listen
) -> None:
    """Not just at today's bin count. `HOP_BINS_LADDER` is what the planner may choose
    from, so every rung is a value the stitcher will be handed."""
    for bins in bands.HOP_BINS_LADDER:
        assert listen.hop_usable_bins(bins) == bands.hop_usable_bins(bins), bins
        # ...and the property both rely on, which is why the floor division is doubled.
        assert listen.hop_usable_bins(bins) % 2 == 0


def test_the_two_place_every_hop_at_the_same_frequency(bands, listen) -> None:
    """The consequence, rather than the constant: the planner says where to tune and the
    stitcher lays the rows out on that assumption. A disagreement here is a row whose
    axis is wrong by a hop's worth of offset."""
    start_hz, stop_hz = 144_000_000, 148_000_000
    plan = bands.hop_plan(start_hz, stop_hz)
    assert plan is not None, "2 m must still be hoppable"
    rate_hz, bins, hops = plan

    assert listen.hop_centres(start_hz, rate_hz, bins, hops) == bands.hop_centres(
        start_hz, plan
    )


def test_the_hops_OVERSHOOT_the_span_they_were_planned_for(bands, listen) -> None:
    """The fact the crop exists for, asserted rather than assumed.

    Whole hops cannot tile an arbitrary span exactly, so the planner rounds UP and the
    stitched row reaches past the stop that was asked for. That is correct as a CAPTURE
    plan — the alternative is a gap at the top of the band — and wrong as a published
    row.
    """
    start_hz, stop_hz = 88_000_000, 108_000_000  # the FM dial
    plan = bands.hop_plan(start_hz, stop_hz)
    assert plan is not None, "the FM dial must still be hoppable"
    rate_hz, bins, hops = plan
    usable = listen.hop_usable_bins(bins)
    bin_hz = rate_hz / bins

    reach = start_hz + usable * hops * bin_hz

    assert reach > stop_hz
    # Not a rounding error: nearly two megahertz above the top of the band, which is
    # where 108.3, 108.7, 109.1, 109.4 and 109.7 were reported as stations.
    assert reach - stop_hz > 1_000_000


def test_a_carrier_that_BLINKS_is_drawn_solid(listen) -> None:
    """The measured defect, and the whole reason this exists.

    A wideband-FM carrier's per-BIN power is not steady and no averaging inside one
    6.8 ms hop can make it so: every segment in that window sees the same instant of the
    modulation, which moves on the audio's timescale. MEASURED on the dial: the band's
    strongest carrier stood up in 14 of 27 rows, swinging 31.6 dB, with ZERO bins
    missing — measured and not measured, row after row, which draws as a dashed line.
    """
    on = np.array([-60.0, -20.0, -60.0])
    off = np.array([-60.0, -58.0, -60.0])

    drawn = [
        listen.held_row([off, on, off, off]),
        listen.held_row([off, off, off, off]),
    ]

    # Any sweep in the window carrying the carrier is enough to draw it.
    assert drawn[0][1] == -20.0
    # ...and once it has left the window entirely, it goes. A hold that never forgets is
    # a picture of everything that ever happened, not of the band.
    assert drawn[1][1] == -58.0


def test_the_hold_keeps_the_STRONGEST_reading_not_the_mean(listen) -> None:
    """A carrier's true level is what it REACHES. A mean of four looks at a sloshing
    signal is just a quieter unsteady number, and the level is what the agent reads."""
    swings = [
        np.array([-40.0]),
        np.array([-12.0]),
        np.array([-38.0]),
        np.array([-35.0]),
    ]

    assert listen.held_row(swings)[0] == -12.0


def test_a_bin_that_measured_NOTHING_never_beats_one_that_did(listen) -> None:
    """`np.fmax`, not `np.maximum`: NaN must lose to a real reading rather than poison
    it. A bin NaN in every sweep stays NaN, which is right — `peaks.find` skips it and
    the PWA paints it transparent, because a bin that measured nothing is not a quiet
    bin and drawing a floor there would claim a measurement nobody took."""
    torn = np.array([np.nan, np.nan])
    real = np.array([-30.0, np.nan])

    out = listen.held_row([torn, real, torn])

    assert out[0] == -30.0
    assert np.isnan(out[1])


def test_the_window_is_what_the_constant_says(listen) -> None:
    """The cost is stated in seconds, so it has to be derivable from the constant: a row
    means "the strongest reading in the last HOLD_SWEEPS sweeps", not "measured now"."""
    assert listen.HOLD_SWEEPS == 4
    # At the dial's measured 2.25 rows a second that is ~1.8 s of separate looks.
    assert 1.0 <= listen.HOLD_SWEEPS / 2.25 <= 2.5
