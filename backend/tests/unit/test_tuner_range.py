"""The api and the sidecar must agree on what the radio can tune.

They cannot share the constant: `deploy/sdr/` ships in its own container and imports
nothing from the backend. So it is duplicated on purpose, and this is the part that
cannot be fixed by sharing a module — a check that the duplicate has not drifted.

It HAD drifted, which is why this exists. `api/sdr.py` and `agent/sdrtools.py` both said
0.024 MHz against the sidecar's 24. A request for anything between passed every bound
the api enforces and came back a 502 from the radio: not a wrong answer but a right one
from the wrong layer, with an error message to match. Two copies out of three were wrong
and nothing noticed, because nothing compared them.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from jbrain.sdr.bands import LIVE_MAX_BINS, MIN_LIVE_BIN_HZ
from jbrain.sdr.tuner import (
    DIRECT_MIN_MHZ,
    MAX_MHZ,
    MAX_SPAN_MHZ,
    MIN_MHZ,
    live_bin_hz,
    nodes_in,
    serials_in,
    sweepable,
    viewable,
)

_SIDECAR = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "sdr" / "listen.py"


def _constant(name: str) -> int:
    """Read one `NAME = 123_456` off the sidecar's source.

    By text, not by import: `deploy/sdr/` modules import each other by bare name because
    they share one WORKDIR in the image, so importing this one from here fails on its
    siblings rather than on anything real."""
    found = re.search(rf"^{name} = ([0-9_]+)$", _SIDECAR.read_text(), re.MULTILINE)
    assert found, f"{name} is not in {_SIDECAR} — did it move?"
    return int(found.group(1).replace("_", ""))


@pytest.mark.skipif(not _SIDECAR.exists(), reason="the sidecar is not in this checkout")
def test_the_api_refuses_exactly_what_the_sidecar_refuses() -> None:
    assert _constant("MIN_HZ") == MIN_MHZ * 1_000_000
    assert _constant("MAX_HZ") == MAX_MHZ * 1_000_000
    # The SECOND range, added when HF landed. It is the same class of duplicate and the
    # same class of silent failure: a sidecar that disagreed here would refuse a
    # shortwave frequency the api had already accepted, as a 502 rather than a bound.
    assert _constant("DIRECT_MIN_HZ") == DIRECT_MIN_MHZ * 1_000_000
    # The waterfall's two bounds, mirrored for the same reason and drifting the same
    # way: a sidecar that carried a smaller frame cap would refuse — as a 502 — a live
    # spectrum the api had already coarsened to fit, and one that allowed a wider span
    # would let the api hand back a picture nothing here believes it asked for.
    assert _constant("SPECTRUM_MAX_BINS") == LIVE_MAX_BINS
    assert _constant("MAX_SWEEP_SPAN_HZ") == MAX_SPAN_MHZ * 1_000_000
    # The bin-width floor the band table's capture ladder is held above. It CLAMPS in
    # the sidecar rather than raising, so a table that drifted under it would ship
    # frames declaring a width the transform never used — a wrong picture nothing
    # contradicts, which is why the two copies are compared here (§6.14).
    assert _constant("MIN_SWEEP_BIN_HZ") == MIN_LIVE_BIN_HZ


def test_no_source_file_writes_the_tuner_range_as_a_literal() -> None:
    """The bound has to be imported, not retyped — which is the only durable version of
    the fix, since the value being wrong was never the point.

    It was retyped FOUR times: `api/sdr.py`, `agent/sdrtools.py`, and twice more as bare
    `Query(gt=0.024, ...)` literals inside `api/debug.py`, found only after the first
    three were consolidated and I had already said the job was done. Sharing a module
    fixes the copies that exist; this fixes the next one."""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "jbrain"
    offenders = [
        f"{path.relative_to(root)}:{n}"
        for path in root.rglob("*.py")
        if path.name != "tuner.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"(?<![\w.])0\.024(?![\w])|(?<![\w.])1766\.0(?![\w])", line)
    ]

    assert offenders == [], f"tuner range hardcoded instead of imported: {offenders}"


class TestReadingTheUsbScan:
    """`serials_in` parses an external payload and decides which radios exist.

    It had no test at all, and its docstring claimed one — the sort of claim that is
    only ever checked by someone going to look. The load-bearing part is that it
    distinguishes "the scan saw nothing" from "the scan could not see": collapsing them
    made a healthy scan reporting zero radios skip the refusal entirely, so a service
    dedicated to an absent dongle started on whatever enumerated first.
    """

    def test_it_reads_the_serials_the_scan_found_IN_ORDER(self) -> None:
        """Sorted, so "the first one" is stable across reboots rather than USB order —
        which is the entire bug this feature removes.

        Enough serials that set iteration does not land on sorted order by luck: with
        two it does, and a version returning `list(set(...))` passed."""
        serials = [
            "A1B2C3D4",
            "77192819",
            "99887766",
            "41550903",
            "09022796",
            "55443322",
            "12345678",
            "ZZ001122",
        ]
        found = serials_in({"sysfs_readable": True, "sdrs": [{"serial": s} for s in serials]})

        assert found == sorted(serials)

    def test_a_scan_that_saw_nothing_is_an_empty_list_not_a_blind_one(self) -> None:
        assert serials_in({"sysfs_readable": True, "sdrs": []}) == []

    def test_a_scan_that_could_not_see_is_None(self) -> None:
        # A supervisor with no /sys mounted answers 200 with sysfs_readable false. That
        # is not "no radios", and treating it as such is how the refusal gets skipped.
        assert serials_in({"sysfs_readable": False, "sdrs": []}) is None
        assert serials_in({"sdrs": [{"serial": "09022796"}]}) is None
        assert serials_in("not a payload") is None
        assert serials_in(None) is None

    def test_a_device_with_no_serial_is_left_out(self) -> None:
        # It cannot be named to `-d`, so listing it would offer a selection that cannot
        # actually be made.
        found = serials_in(
            {"sysfs_readable": True, "sdrs": [{"serial": ""}, {}, {"serial": 5}, {"serial": "x1"}]}
        )

        assert found == ["x1"]

    def test_the_same_radio_twice_is_one_radio(self) -> None:
        payload = {"sysfs_readable": True, "sdrs": [{"serial": "x1"}, {"serial": "x1"}]}

        assert serials_in(payload) == ["x1"]


class TestWhatALiveViewCanCover:
    """A waterfall is no longer rtl_power on every tier, and this is where that shows.

    The one-hop picture reads raw I/Q and does its own FFT, which reaches shortwave
    through direct sampling mode 2 — the ADC branch this board wires and the one
    `rtl_power -D` cannot select. So `viewable` and `sweepable` stopped being the same
    question, and the refusals below 24 MHz became narrower rather than absent.
    """

    def test_a_row_too_fine_to_send_is_coarsened_by_doubling(self) -> None:
        # rtl_power's ladder, and only its: the multi-hop tier is still swept by the
        # tool, which grants the largest power-of-two division of its per-hop bandwidth
        # no coarser than what it was asked for. An exact quotient is a number it will
        # not honour — which is exactly why the one-hop tier no longer comes here.
        assert live_bin_hz(4_000_000, 100) == 1_600
        assert LIVE_MAX_BINS >= 4_000_000 // 1_600
        # ...and 800 really was too fine.
        assert LIVE_MAX_BINS < 4_000_000 // 800

    def test_a_bin_that_already_fits_is_left_alone(self) -> None:
        assert live_bin_hz(4_000_000, 25_000) == 25_000

    def test_a_nonsense_bin_does_not_divide_by_zero(self) -> None:
        assert live_bin_hz(4_000_000, 0) > 0

    def test_shortwave_can_now_be_drawn_though_it_still_cannot_be_swept(self) -> None:
        """The whole of F8 in two lines. The same 300 kHz of 40 m is a picture and not
        a survey, because the two run different engines and only one of them can be put
        into the ADC mode this board needs."""
        assert viewable(7.0, 7.3) is None

        refusal = sweepable(7.0)
        assert refusal and "still listen" in refusal

    def test_shortwave_wider_than_one_capture_is_refused_in_those_words(self) -> None:
        """Not "cannot be swept" — that is no longer why. Below 24 MHz the picture is
        one capture or nothing, because the thing that stitches hops together is the
        tool that cannot go down there."""
        refusal = viewable(3.0, 8.0)

        assert refusal and "more than one capture" in refusal

    def test_a_range_that_crosses_the_tuner_floor_says_which_line_it_crossed(
        self,
    ) -> None:
        # BOTH edges, and for a reason bandwidth cannot express: the tuner is powered
        # down on one side of 24 MHz and in circuit on the other, so no single capture
        # covers both halves however narrow the range is.
        refusal = viewable(10.0, 30.0)

        assert refusal and "changes signal path" in refusal
        assert viewable(23.9, 24.1) is not None

    def test_an_edge_in_the_second_nyquist_zone_is_refused_by_name(self) -> None:
        """14.4-24 MHz passes every bound and is reachable by neither path: the tuner
        is bypassed down there and direct sampling folds `28.8 − f` onto it. A range
        starting at 20 MHz would be drawn as one starting at 8.8, so the refusal says
        which frequency the radio would really have given."""
        refusal = viewable(20.0, 30.0)

        assert refusal and "8.8 MHz" in refusal

    def test_an_edge_the_radio_cannot_reach_at_all_is_still_caught(self) -> None:
        assert viewable(30.0, 1800.0) is not None

    def test_a_span_wider_than_the_sweep_allows_names_the_ceiling(self) -> None:
        refusal = viewable(400.0, 500.0)

        assert refusal and f"{MAX_SPAN_MHZ:g}" in refusal

    def test_a_range_that_is_not_a_range_is_refused(self) -> None:
        assert viewable(146.0, 146.0) is not None
        assert viewable(148.0, 144.0) is not None

    def test_an_ordinary_band_is_viewable(self) -> None:
        assert viewable(144.0, 148.0) is None


class TestFindingTheDeviceToReset:
    """Serial -> device node, and why it comes from sysfs.

    A reset is aimed at a dongle that has stopped answering — which is exactly the
    device librtlsdr can no longer identify. sysfs answers anyway, from what the kernel
    cached when the device first enumerated, and that is the whole reason a remote reset
    is possible at all.
    """

    def test_it_maps_every_radio_the_scan_named(self) -> None:
        found = nodes_in(
            {
                "sysfs_readable": True,
                "sdrs": [
                    {"serial": "09022796", "device_node": "/dev/bus/usb/001/011"},
                    {"serial": "77192819", "device_node": "/dev/bus/usb/003/010"},
                ],
            }
        )

        assert found == {
            "09022796": "/dev/bus/usb/001/011",
            "77192819": "/dev/bus/usb/003/010",
        }

    def test_a_scan_that_could_not_see_maps_nothing(self) -> None:
        # Unlike `serials_in`, "we could not see" and "no such radio" are the SAME
        # answer here: a reset needs a node, and there is no node either way. Guessing
        # one would aim an ioctl that re-enumerates hardware at a device nobody named.
        assert nodes_in({"sysfs_readable": False, "sdrs": []}) == {}
        assert nodes_in({"sdrs": [{"serial": "x", "device_node": "/dev/bus/usb/001/011"}]}) == {}
        assert nodes_in("not a payload") == {}
        assert nodes_in(None) == {}

    def test_a_radio_with_no_node_is_left_out(self) -> None:
        found = nodes_in(
            {
                "sysfs_readable": True,
                "sdrs": [
                    {"serial": "a1", "device_node": ""},
                    {"serial": "a2"},
                    {"serial": "", "device_node": "/dev/bus/usb/001/011"},
                    {"serial": "a3", "device_node": "/dev/bus/usb/001/012"},
                ],
            }
        )

        assert found == {"a3": "/dev/bus/usb/001/012"}
