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
