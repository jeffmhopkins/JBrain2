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

from jbrain.sdr import bands
from jbrain.sdr.bands import LIVE_MAX_BINS, MIN_LIVE_BIN_HZ
from jbrain.sdr.tuner import (
    CONVERTER_MAX_MHZ,
    CONVERTER_MIN_MHZ,
    DIRECT_MIN_MHZ,
    MAX_MHZ,
    MAX_SPAN_MHZ,
    MIN_MHZ,
    direct_sampling,
    nodes_in,
    out_of_range,
    serials_in,
    tuned_mhz,
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
    """One engine draws and integrates, so `viewable` is the only question.

    It reads raw I/Q and does its own FFT, which reaches shortwave through direct
    sampling mode 2 — the ADC branch this board wires and the one `rtl_power -D` cannot
    select. `sweepable` was that tool's separate answer and went with it (B1/B2); what
    is left below 24 MHz is a narrower rule about captures, not about tools.
    """

    def test_a_span_no_capture_plan_covers_is_refused_in_words(self) -> None:
        """B1: the tier that used to catch this was `rtl_power`, which drew the range
        on its own uncalibrated scale. There is no second engine now, so the limit is
        the hop plan's and it is said rather than worked around — and the number comes
        off the same ladder `hop_plan` walks, so the two cannot drift."""
        widest = bands.widest_stitchable_hz() / 1_000_000

        assert viewable(144.0, 144.0 + widest - 1) is None
        refusal = viewable(144.0, 144.0 + widest + 1)
        assert refusal is not None
        assert "stitch in one row" in refusal
        assert f"{widest:g}" in refusal

    def test_every_curated_section_is_inside_that_limit(self) -> None:
        """What the refusal above must never catch: a band button in the sheet."""
        for section in bands.SECTIONS:
            assert viewable(section.start_hz / 1e6, section.stop_hz / 1e6) is None, section.id

    def test_shortwave_is_both_drawable_and_surveyable_now(self) -> None:
        """This assertion spent three waves being the OPPOSITE. The same 300 kHz of 40 m
        was a picture and not a survey, because the two ran different engines and only
        one could be put into the ADC mode this board wires. B2 put the survey on the
        picture's engine, so there is one predicate and one answer."""
        assert viewable(7.0, 7.3) is None

    def test_shortwave_wider_than_one_capture_is_refused_in_those_words(self) -> None:
        """Not "cannot be swept" — that is no longer why. Below 24 MHz a picture is one
        capture or nothing, because every hop would have to satisfy the Nyquist window
        separately down there (`bands.hop_plan`)."""
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


class TestWhatAConverterChangesAboutTheRange:
    """An upconverter moves the whole question onto the TUNER path.

    It is not "unlocking HF" — direct sampling already reaches 0.1 MHz, and the module
    docstring above says so. What it buys is the tuner being in circuit down there: a
    gain control at all, no `28.8 − f` image summed into every bin, and no 14.4-24 MHz
    hole. The range check has to follow, or the api refuses a frequency the hardware
    would tune perfectly.
    """

    UP = 125.0

    def test_no_converter_leaves_every_answer_exactly_where_it_was(self) -> None:
        """The default, and the one that must not move: a radio nobody has configured
        meets the checks it has always met."""
        for mhz in (0.05, 0.53, 7.2, 14.4, 18.1, 24.0, 146.52, 1766.0, 1800.0):
            assert out_of_range(mhz, 0.0) == out_of_range(mhz), mhz
            assert direct_sampling(mhz, 0.0) == direct_sampling(mhz), mhz
            assert tuned_mhz(mhz) == mhz, mhz

    def test_the_hole_is_gone_because_the_tuner_is_back_in_circuit(self) -> None:
        # 18.1 bare folds onto 10.7 and is refused; through the converter it is 143.1,
        # which is an ordinary tuning.
        assert out_of_range(18.1) is not None
        assert out_of_range(18.1, self.UP) is None
        assert direct_sampling(18.1, self.UP) is False

    def test_an_offset_too_small_for_the_band_is_refused_as_an_OFFSET_problem(
        self,
    ) -> None:
        """Saying which is the difference between an owner correcting a setting and an
        owner concluding the radio cannot hear a band it can."""
        said = out_of_range(7.2, 10.0)

        assert said and "offset is too small" in said
        assert "17.2 MHz" in said and "7.2 MHz" in said

    def test_a_converter_does_not_excuse_a_tune_past_the_top(self) -> None:
        """The dial moved to 60 MHz and the offset to 1800 because the CONVERTER's own
        input is now asked about first: 1700 MHz never reaches this branch, since a
        Ham It Up passes nothing like it (`TestWhatTheConverterItselfPasses`). What is
        left here is the case the offset alone creates — a dial the converter passes,
        shifted past the dongle's ceiling."""
        said = out_of_range(60.0, 1800.0)

        assert said and "1860 MHz" in said and "60 MHz" in said

    def test_a_converted_span_is_planned_against_the_tuner_path(self) -> None:
        """40 m through a converter is the R820T2 doing ordinary work at 132 MHz, so the
        span is judged by the tuner's rules rather than by the ADC branch its own edges
        would imply. Both answers are "yes" here — what matters is that the second one
        is reached at all, since the direct-path branch refuses anything with no single
        capture plan below 24 MHz."""
        assert viewable(7.0, 7.3) is None
        assert viewable(7.0, 7.3, self.UP) is None
        # ...and the tuner path's own width limit applies to it, which the direct path's
        # rule would have phrased entirely differently. It starts at 1.0 rather than 7.0
        # so the span stays inside the converter's own input (0.0003-65 MHz), which is
        # asked first now: 7-68 would be refused for running past it instead.
        wide = viewable(1.0, 1.0 + MAX_SPAN_MHZ + 1, self.UP)
        assert wide and "wider than the radio can sweep" in wide


class TestWhatTheConverterItselfPasses:
    """The bound that is about the ACCESSORY, not about the dongle.

    Every other check in `tuner` asks what the RTL-SDR can do, and with a converter
    inline it asks that of the tune — which is right, and was the whole answer. The
    owner set 125 MHz inline on the long wire and swept FM broadcast: 98 + 125 is
    223 MHz, an ordinary tuning, so the request was accepted and came back as noise.
    Nothing had asked whether the converter's input passed 98 MHz. It does not: this
    Ham It Up passes 300 Hz to 65 MHz, a figure the owner supplied on 2026-09-12.
    """

    UP = 125.0

    def test_the_FM_band_through_a_converter_is_refused_and_says_why(self) -> None:
        """The incident, as one assertion. Both numbers appear for the reason the rest
        of this module names both — an owner reading about 223 MHz after asking for 98
        has no way to connect the two — and the remedy is the one that works, since
        88-108 is an ordinary tuning with the converter Off."""
        said = out_of_range(98.0, self.UP)

        assert said is not None
        assert "converter" in said
        assert "98 MHz" in said and "223 MHz" in said
        assert f"{CONVERTER_MAX_MHZ:g} MHz" in said
        assert "turn the converter Off" in said

    def test_the_top_edge_passes_and_just_above_it_does_not(self) -> None:
        assert out_of_range(CONVERTER_MAX_MHZ, self.UP) is None
        assert out_of_range(CONVERTER_MAX_MHZ + 0.001, self.UP) is not None

    def test_the_bottom_edge_passes_and_just_below_it_does_not(self) -> None:
        assert out_of_range(CONVERTER_MIN_MHZ, self.UP) is None
        assert out_of_range(CONVERTER_MIN_MHZ / 2, self.UP) is not None

    def test_the_bottom_edge_is_300_Hz_and_has_not_become_zero(self) -> None:
        """300 Hz is 0.0003 MHz, and the failure mode of a sub-kHz bound in a module
        that speaks MHz is that it rounds to nothing — at which point the floor silently
        stops existing and this file's other assertions all still pass."""
        assert pytest.approx(300.0) == CONVERTER_MIN_MHZ * 1_000_000
        assert int(CONVERTER_MIN_MHZ) == 0 and CONVERTER_MIN_MHZ > 0

    def test_a_dial_the_converter_blocks_is_refused_before_the_dongles_ceiling(
        self,
    ) -> None:
        """Two things are wrong with 1700 MHz through a 125 MHz converter and only one
        of them has a fix: the tune is past 1766, and the converter passed none of it.
        Told the first, an owner concludes the radio cannot reach 1700 — it can, with
        the converter Off."""
        said = out_of_range(1700.0, self.UP)

        assert said and "converter passes" in said and "turn the converter Off" in said

    def test_the_low_refusal_offers_no_remedy_because_there_is_none(self) -> None:
        # Below 0.0003 MHz the converter is not what is in the way for long: with it Off
        # the dial is under `DIRECT_MIN_MHZ` as well, so "turn it off" would be a fix
        # that fails, and the sentence stops at the fact.
        said = out_of_range(0.0001, self.UP)

        assert said and "converter passes" in said
        assert "turn the converter Off" not in said

    def test_no_converter_is_refused_exactly_nothing_new(self) -> None:
        """The passband must not narrow a radio with `upconverter_hz: 0` — which is
        every radio until someone says otherwise, and both of this box's until today."""
        for mhz in (0.0001, 0.0003, 0.1, 7.2, 65.0, 65.001, 88.0, 98.0, 108.0, 1766.0, 1800.0):
            assert out_of_range(mhz, 0.0) == out_of_range(mhz), mhz
        # The FM band specifically: the thing this check refuses THROUGH a converter is
        # as tunable bare as it has always been.
        for mhz in (88.0, 98.0, 108.0):
            assert out_of_range(mhz) is None, mhz
        assert viewable(88.0, 108.0) is None

    def test_a_span_wholly_outside_the_converter_is_refused_as_a_span(self) -> None:
        """The FM sweep that started this. A sentence about a range, not about one of
        its edges, because a range is what was asked for."""
        said = viewable(88.0, 108.0, self.UP)

        assert said is not None
        assert "88-108 MHz" in said and "converter passes" in said
        assert "turn the converter Off" in said

    def test_a_span_only_partly_inside_it_is_refused_whole_and_says_so(self) -> None:
        """REFUSED, NOT TRIMMED, and the message has to say which: a trimmed 60-65 would
        come back labelled 60-70, and a quiet stripe where the converter stops is
        indistinguishable from a quiet band once it is drawn."""
        said = viewable(60.0, 70.0, self.UP)

        assert said is not None
        assert "refused whole rather than trimmed" in said
        assert f"60-{CONVERTER_MAX_MHZ:g} MHz" in said

    def test_the_overlap_it_names_is_the_one_that_would_be_accepted(self) -> None:
        assert viewable(60.0, CONVERTER_MAX_MHZ, self.UP) is None

    def test_a_span_hanging_off_the_bottom_edge_names_that_overlap_instead(self) -> None:
        said = viewable(0.0001, 5.0, self.UP)

        assert said is not None
        assert "refused whole rather than trimmed" in said
        assert f"{CONVERTER_MIN_MHZ:g}-5 MHz" in said

    def test_a_span_the_converter_passes_is_judged_by_the_radios_own_rules(self) -> None:
        # The check is a gate in front of the existing ones, not a replacement for them.
        assert viewable(7.0, 7.3, self.UP) is None
