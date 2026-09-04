"""The band table, and the arithmetic that keeps it honest.

A wrong row here is a lie nothing else contradicts. A section whose `live` claims one
hop but spans three silently hands the owner a fifth of the sensitivity in the same
colours; a wrong `channel_hz` unfolds one signal into forty stations, or computes the
steady-carrier baseline from inside a carrier so the carrier hides. None of that raises
an error, and none of it is visible on the screen it damages — so the arithmetic runs
here instead, over the rows we actually ship.
"""

from __future__ import annotations

import pytest

from jbrain.sdr import bands
from jbrain.sdr.bands import LIVE_FAST, LIVE_NONE, LIVE_SLOW, Channel, Section


def test_the_shipped_table_agrees_with_the_physics() -> None:
    """The gate. Every rule in `validate` re-derives something from the row rather than
    trusting it, so this fails when a section is edited into something the hardware
    cannot do — which is the only way that error would ever be noticed."""
    assert bands.validate() == []


def test_there_are_enough_sections_to_be_worth_a_picker() -> None:
    # Guards against the table being emptied or half-imported without anything failing.
    assert len(bands.SECTIONS) > 20


class TestTheValidatorActuallyCatchesThings:
    """`validate()` is the only thing standing between a typo and a mis-tuned radio, so
    each of its rules is tested against a row that breaks it. A validator nobody has
    seen fail is a validator that might not run at all."""

    def _row(self, **over: object) -> Section:
        base: dict[str, object] = {
            "id": "x",
            "band": "B",
            "name": "N",
            "start_hz": 144_000_000,
            "stop_hz": 145_000_000,
            "mode": "fm",
            "step_hz": 5_000,
            "channel_hz": 15_000,
        }
        return Section(**{**base, **over})  # type: ignore[arg-type]

    def test_a_live_view_wider_than_one_hop_is_refused(self) -> None:
        """The load-bearing rule. Above ~2 MHz the receiver must retune to cover the
        span, which drops the duty cycle from 100% to 20% — same band, same colours,
        five times less likely to catch a short transmission."""
        bad = self._row(stop_hz=148_000_000, live=LIVE_FAST, sweep_seconds=300)

        assert any("needs a hop" in p for p in bands.validate((bad,)))

    def test_a_multi_hop_sweep_must_run_longer(self) -> None:
        bad = self._row(stop_hz=150_000_000, live=LIVE_NONE, sweep_seconds=60)

        assert any("fraction of the time" in p for p in bands.validate((bad,)))

    def test_a_band_of_continuous_carriers_is_exempt_from_that(self) -> None:
        """Duty cycle only matters if a signal can be missed between visits. Broadcast
        carriers are never off, so a low duty cycle costs nothing there."""
        ok = self._row(stop_hz=150_000_000, live=LIVE_NONE, sweep_seconds=60, continuous=True)

        assert bands.validate((ok,)) == []

    def test_bins_coarser_than_the_channel_grid_are_refused(self) -> None:
        """One bin straddling two channels reports them as one signal."""
        bad = self._row(sweep_bin_hz=25_000, channel_hz=15_000)

        assert any("straddle" in p for p in bands.validate((bad,)))

    def test_a_step_that_walks_off_the_channel_grid_is_refused(self) -> None:
        bad = self._row(step_hz=7_000, channel_hz=15_000)

        assert any("off the grid" in p for p in bands.validate((bad,)))

    def test_a_named_channel_outside_its_own_section_is_refused(self) -> None:
        """Caught a real error in the first draft: 121.500 Guard was listed inside a
        section that stopped at 121.000."""
        bad = self._row(channels=(Channel(999_000_000, "Nowhere"),))

        assert any("outside it" in p for p in bands.validate((bad,)))

    def test_an_HF_section_cannot_claim_the_rtl_power_live_tier(self) -> None:
        """`rtl_power -D` hardcodes direct sampling mode 1 — the I branch — and this
        hardware wires the Q branch. So the slow live tier, which IS rtl_power, cannot
        reach HF at all, however wide the section is."""
        bad = self._row(start_hz=7_000_000, stop_hz=7_300_000, live=LIVE_SLOW, channel_hz=0)

        assert any("wrong ADC branch" in p for p in bands.validate((bad,)))

    def test_duplicate_ids_are_refused(self) -> None:
        assert any("duplicate id" in p for p in bands.validate((self._row(), self._row())))


class TestWhatTheHardwareCanReach:
    def test_every_HF_section_says_it_cannot_be_surveyed(self) -> None:
        """Not a preference. rtl_power cannot be put into the mode this dongle needs, so
        HF listening works and HF sweeping does not — and the table has to carry that or
        the owner meets it as a failed job."""
        for s in bands.SECTIONS:
            assert s.surveyable is (s.start_hz > bands.DIRECT_SAMPLING_MAX_HZ), s.id

    def test_nothing_below_the_tuner_claims_to_use_it(self) -> None:
        hf = [s for s in bands.SECTIONS if s.direct_sampling]
        assert hf, "the table lost its HF sections"
        for s in hf:
            assert s.stop_hz <= bands.DIRECT_SAMPLING_MAX_HZ

    def test_every_HF_section_says_which_band_folds_onto_it(self) -> None:
        """`mirrored` was False for all ten HF rows and told the owner nothing, while
        all ten carry an image: the ADC samples at 28.8 MHz, so everything at 28.8 − f
        is summed into the same bins, reversed. Not separable in software — which is
        why the honest answer is to name the band rather than to raise a flag."""
        forty = bands.by_id("sw-41m")
        assert forty is not None
        # 7.200-7.450 folds with 21.350-21.600: 15 m amateur and 13 m broadcast.
        assert (forty.image_start_hz, forty.image_stop_hz) == (21_350_000, 21_600_000)
        for s in bands.SECTIONS:
            if s.direct_sampling:
                assert s.image_start_hz == bands.ADC_RATE_HZ - s.stop_hz, s.id
                assert s.image_stop_hz == bands.ADC_RATE_HZ - s.start_hz, s.id
            else:
                # Above 24 MHz the tuner is in circuit and there is no fold to report.
                assert (s.image_start_hz, s.image_stop_hz) == (0, 0), s.id

    def test_the_duty_cycle_falls_with_hop_count(self) -> None:
        """The number that justifies splitting bands at all."""
        one = bands.Section(
            id="a",
            band="B",
            name="N",
            start_hz=144_000_000,
            stop_hz=146_000_000,
            mode="fm",
            step_hz=5_000,
            channel_hz=15_000,
        )
        many = bands.Section(
            id="b",
            band="B",
            name="N",
            start_hz=88_000_000,
            stop_hz=108_000_000,
            mode="fm",
            step_hz=5_000,
            channel_hz=15_000,
        )

        assert one.hops == 1 and one.duty == 1.0
        assert many.hops == 8 and many.duty < 0.15


class TestTheCaptureBehindALiveRow:
    """The rate and the bin count, which are now this table's to choose.

    Every rule here fails SILENTLY on the box. librtlsdr quantises a sample rate to a
    22-bit divider and tells nobody; `Sweep.of` clamps a bin width that is too fine
    rather than refusing it; and a direct capture that reaches past 14.4 MHz folds the
    band onto itself inside one frame. All three produce a picture that is wrong rather
    than absent, which is the only kind of error this file exists for.
    """

    def _fast(self, **over: object) -> Section:
        base: dict[str, object] = {
            "id": "x",
            "band": "B",
            "name": "N",
            "start_hz": 144_000_000,
            "stop_hz": 145_000_000,
            "mode": "fm",
            "step_hz": 5_000,
            "channel_hz": 15_000,
            "live": LIVE_FAST,
            "sample_rate_hz": 1_600_000,
        }
        return Section(**{**base, **over})  # type: ignore[arg-type]

    def test_the_shipped_ladder_is_exactly_achievable(self) -> None:
        """The headline claim. Every rate here comes back off the divider unchanged, so
        `bin_hz = rate / N` is a constant rather than a number that flaps."""
        for rate, _bins in bands.LIVE_CAPTURES:
            assert bands.achieved_rate_hz(rate) == rate, rate

    def test_the_rates_the_plan_rejected_really_are_inexact(self) -> None:
        """The arithmetic that chose the ladder, run rather than quoted. 1.5 MS/s and
        250 kS/s are the two the earlier drafts wanted and the divider will not give."""
        assert bands.achieved_rate_hz(1_500_000) == pytest.approx(1_500_000.0149, abs=1e-4)
        assert bands.achieved_rate_hz(250_000) == pytest.approx(250_000.0004, abs=1e-4)
        assert bands.rate_is_exact(2_048_000) and bands.rate_is_exact(256_000)

    def test_900_kilosamples_does_not_exist(self) -> None:
        """The trap the third draft fell into twice: `rtlsdr_set_sample_rate` rejects
        `rate > 300000 && rate <= 900000`, so 900 kS/s is inside the excluded band. The
        usable neighbour is 1.024 MS/s."""
        assert not bands.rate_is_legal(900_000)
        assert bands.rate_is_legal(900_001) and bands.rate_is_legal(1_024_000)
        assert not bands.rate_is_legal(225_000) and bands.rate_is_legal(225_001)
        assert not bands.rate_is_legal(3_200_001)

    def test_every_live_row_carries_an_exact_bin_width_above_the_floor(self) -> None:
        """`MIN_SWEEP_BIN_HZ` CLAMPS instead of raising, so a row whose arithmetic came
        out at 61 Hz would ship frames declaring 100 Hz bins the FFT never used — worse
        than a refusal, and invisible. At a fixed N=4096 five sections would do exactly
        that; the bin count follows the rate instead."""
        for s in bands.SECTIONS:
            if s.live != LIVE_FAST:
                assert s.live_bin_hz == 0, s.id
                continue
            assert isinstance(s.live_bin_hz, int), s.id  # exact, not a float
            assert s.live_bin_hz >= bands.MIN_LIVE_BIN_HZ, s.id
            assert s.fft_bins <= bands.LIVE_MAX_BINS, s.id

    def test_every_HF_capture_stays_inside_the_honest_window(self) -> None:
        """`R/2 <= fc <= 14.4 MHz - R/2`. Below 0 Hz the picture is a mirror of what is
        just above it; past 14.4 MHz the next Nyquist zone folds into the same frame."""
        for s in bands.SECTIONS:
            if s.direct_sampling and s.sample_rate_hz:
                assert bands.window_holds(s.sample_rate_hz, s.centre_hz), s.id
                assert s.capture_start_hz > 0, s.id
                assert s.capture_stop_hz <= bands.NYQUIST_HZ, s.id

    def test_a_rate_the_driver_would_quantise_is_refused(self) -> None:
        assert any("flaps" in p for p in bands.ladder_problems(((1_500_000, 4_096),)))

    def test_an_illegal_rate_is_refused(self) -> None:
        assert any("librtlsdr accepts" in p for p in bands.ladder_problems(((900_000, 4_096),)))

    def test_a_bin_width_that_does_not_divide_is_refused(self) -> None:
        """2.4 MS/s over 4096 bins is 585.9375 Hz. Rounded to 586 the top of the frame
        is 256 Hz out, and nothing downstream can tell."""
        bad = bands.ladder_problems(((2_400_000, 4_096),))

        assert any("585.9375" in p and "not exact" in p for p in bad)

    def test_a_bin_finer_than_the_clamp_is_refused(self) -> None:
        bad = bands.ladder_problems(((256_000, 3_200),))

        assert any("CLAMPS" in p for p in bad)

    def test_an_FFT_size_that_is_not_5_smooth_is_refused(self) -> None:
        """N need not be a power of two — that freedom is what makes 600 Hz bins
        possible — but pocketfft is only fast on 5-smooth sizes."""
        assert any("5-smooth" in p for p in bands.ladder_problems(((2_400_000, 4_001),)))

    def test_a_fast_section_with_no_rate_is_refused(self) -> None:
        bad = bands.validate((self._fast(sample_rate_hz=0),))

        assert any("no sample_rate_hz" in p for p in bad)

    def test_an_rtl_power_tier_may_not_name_a_rate(self) -> None:
        """`slow` and `none` are rtl_power, which picks its own rate: a number here
        would describe a capture nothing makes."""
        row = self._fast(live=LIVE_SLOW, sample_rate_hz=2_400_000)

        assert any("chooses its own rate" in p for p in bands.validate((row,)))

    def test_a_rate_off_the_ladder_is_refused(self) -> None:
        bad = bands.validate((self._fast(sample_rate_hz=1_200_000),))

        assert any("capture ladder" in p for p in bad)

    def test_a_band_wider_than_the_trusted_fill_is_refused(self) -> None:
        """The rolloff rule, generalised off `LIVE_FAST_MAX_HZ`: a section that fills
        its own passband reads as dead at both edges."""
        row = self._fast(stop_hz=146_000_000, sample_rate_hz=2_048_000)

        assert any("IF rolloff" in p for p in bands.validate((row,)))

    def test_a_direct_capture_that_folds_the_band_is_refused(self) -> None:
        """20 m at 1.024 MS/s reaches 14.762 MHz, so 14.4-14.76 lands back on
        14.04-14.4 inside the same picture."""
        row = self._fast(
            start_hz=14_150_000, stop_hz=14_350_000, sample_rate_hz=1_024_000, channel_hz=0
        )

        assert any("fold of itself" in p for p in bands.validate((row,)))

    def test_a_tuner_path_section_may_not_use_an_HF_rate(self) -> None:
        """Below the 300-900 kHz legal gap the R820T2's IF filter can no longer bracket
        the picture, and `rtl_fm` keeps the ADC above a megasample for the same reason.
        Those rates are for the direct path only."""
        bad = bands.validate((self._fast(sample_rate_hz=256_000),))

        assert any("IF filter can bracket" in p for p in bad)


class TestChoosingACaptureForAHandEnteredRange:
    """The expert path takes the same ladder a curated section does, so typing
    144.0-144.2 and tapping the 2 m SSB button produce the same picture."""

    def test_it_takes_the_smallest_capture_that_covers_the_range(self) -> None:
        assert bands.capture_for(144_000_000, 146_000_000) == (2_400_000, 4_000)
        assert bands.capture_for(144_000_000, 144_200_000) == (1_024_000, 4_096)

    def test_a_range_wider_than_one_hop_has_no_capture_at_all(self) -> None:
        """Not a refusal — that range is the rtl_power tier's, several hops at one row
        a second, with the duty cost said out loud."""
        assert bands.capture_for(88_000_000, 108_000_000) is None

    def test_shortwave_gets_a_narrow_capture_and_a_window_check(self) -> None:
        twenty = bands.by_id("20m")
        assert twenty is not None
        # The same answer the table stores: the window leaves no room for more.
        assert bands.capture_for(twenty.start_hz, twenty.stop_hz) == (256_000, 1_024)
        # ...and a range that cannot be captured without folding has none.
        assert bands.capture_for(14_300_000, 14_390_000) is None

    def test_the_tuner_path_never_gets_an_HF_rate(self) -> None:
        rate, _bins = bands.capture_for(144_000_000, 144_100_000) or (0, 0)

        assert rate == 1_024_000  # not 256_000, though the span would fit

    def test_a_range_straddling_the_tuner_floor_has_no_capture(self) -> None:
        """23.9-24.1 MHz is not a bandwidth problem: the tuner is powered down on one
        side of 24 MHz and in circuit on the other, so no single capture covers both."""
        assert bands.capture_for(23_900_000, 24_100_000) is None


class TestFindingTheSectionForAFrequency:
    """`containing` is what gives expert mode its defaults: a manually entered frequency
    inherits the mode, step and spacing of wherever it lands, so typing a number does
    not mean choosing six settings by hand."""

    @pytest.mark.parametrize(
        ("hz", "expect_id", "expect_mode"),
        [
            (bands.APRS_HZ, "2m-aprs", "fm"),
            (146_520_000, "2m-repeaters", "fm"),
            (121_500_000, "air-guard", "am"),
            (10_000_000, "wwv-10", "am"),
            (92_300_000, "fm-broadcast", "wbfm"),
            (144_200_000, "2m-ssb", "usb"),
        ],
    )
    def test_it_finds_the_right_section(self, hz: int, expect_id: str, expect_mode: str) -> None:
        found = bands.containing(hz)

        assert found is not None and found.id == expect_id
        assert found.mode == expect_mode

    def test_it_prefers_the_NARROWEST_section_covering_a_frequency(self) -> None:
        """The wide survey rows overlap the specific ones. Someone asking what they are
        listening to wants "2 m repeater outputs", not "2 m whole band" — and crucially
        wants the narrow row's settings, not the wide row's."""
        found = bands.containing(146_940_000)

        assert found is not None and found.id == "2m-repeaters"
        assert bands.by_id("2m-all") is not None  # ...and the wide row does still exist

    def test_a_frequency_in_no_section_is_None_rather_than_a_guess(self) -> None:
        """Between the bands there is genuinely nothing to say, and inventing defaults
        there would be worse than admitting it."""
        assert bands.containing(300_000_000) is None

    def test_the_APRS_channel_is_named_once(self) -> None:
        """It was a float literal in two files. The table owns it now."""
        assert bands.APRS_HZ == 144_390_000
        section = bands.containing(bands.APRS_HZ)
        assert section is not None
        assert any(c.hz == bands.APRS_HZ for c in section.channels)


class TestTheRouteThatServesTheTable:
    """`GET /api/sdr/bands`. Static data, but the shape is a contract: the client is
    supposed to never recompute any of the physics, so every derived fact has to
    actually arrive."""

    async def _out(self):
        from jbrain.api import sdr as sdr_api

        return await sdr_api.band_sections(object())  # type: ignore[arg-type]

    async def test_it_serves_every_section(self) -> None:
        out = await self._out()

        assert [s.id for s in out.sections] == [s.id for s in bands.SECTIONS]
        assert out.region == bands.REGION

    async def test_it_carries_the_derived_facts_so_the_client_never_derives_them(
        self,
    ) -> None:
        """A screen that worked out its own hop count would be a second implementation
        of the physics, free to disagree with the sweep that actually runs."""
        out = await self._out()
        aprs = next(s for s in out.sections if s.id == "2m-aprs")
        whole = next(s for s in out.sections if s.id == "2m-all")

        assert (aprs.span_hz, aprs.hops, aprs.centre_hz) == (800_000, 1, 144_700_000)
        assert whole.hops == 2  # 4 MHz needs a retune, and the client must not guess

    async def test_HF_sections_arrive_marked_unsweepable_and_gainless(self) -> None:
        """Both facts have to reach the screen or it will offer controls that cannot
        work: a Survey button that always fails, and a gain slider wired to a tuner that
        is powered down."""
        out = await self._out()
        wwv = next(s for s in out.sections if s.id == "wwv-10")

        assert wwv.direct_sampling is True
        assert wwv.surveyable is False
        # ...and the capture that WILL draw it, which is the other half of the same
        # honesty: 256 kS/s over 1024 bins is 250 Hz, exactly, and the image it carries
        # is named rather than flagged.
        assert (wwv.sample_rate_hz, wwv.fft_bins, wwv.bin_hz) == (256_000, 1_024, 250)
        assert (wwv.image_start_hz, wwv.image_stop_hz) == (18_700_000, 18_900_000)

    async def test_it_reports_the_two_ranges_the_hardware_actually_has(self) -> None:
        """Not one range with a lower floor. Below `direct_max_hz` the tuner is bypassed
        entirely — a different signal path, not a wider one."""
        out = await self._out()

        assert out.direct_max_hz == bands.DIRECT_SAMPLING_MAX_HZ
        assert out.tuner_min_hz == 24_000_000
        assert out.tuner_max_hz == 1_766_000_000

    async def test_named_channels_survive_the_trip(self) -> None:
        out = await self._out()
        marine = next(s for s in out.sections if s.id == "marine")

        assert any(c.name == "Ch 16" and c.hz == 156_800_000 for c in marine.channels)
        assert any("not voice" in c.note for c in marine.channels)  # Ch 70 is DSC data


class TestTheExpertPathCoversTheWholeRadio:
    """Typing a frequency has to work everywhere the radio reaches, not only inside the
    32 curated sections. Most of the spectrum is not in the table and never will be."""

    def test_every_reachable_frequency_gets_a_mode_and_a_step(self) -> None:
        """Swept across the whole range rather than spot-checked, because the failure
        this guards is a hole between fallback bands, which a handful of samples would
        step straight over."""
        hz = 100_000
        while hz <= 1_766_000_000:
            mode, step, _chan = bands.defaults_for(hz)
            assert mode, hz
            assert step > 0, hz
            hz += 137_000  # a prime-ish stride, so the walk cannot land only on edges

    def test_a_curated_section_wins_over_the_convention(self) -> None:
        """The table is the better answer wherever it has one — 2 m steps in 5 kHz,
        which no general rule would produce."""
        assert bands.defaults_for(146_940_000) == ("fm", 5_000, 15_000)

    def test_airband_gets_AM_even_though_nothing_curated_covers_all_of_it(self) -> None:
        """The commonest wrong choice here is SILENT: FM on an airband frequency gives
        a hiss with a voice buried in it that never resolves, which reads as a dead
        channel rather than a wrong setting. 133.5 is inside the band and outside every
        section this table happens to carry."""
        mode, step, _ = bands.defaults_for(133_500_000)

        assert mode == "am"
        assert step == 25_000

    def test_the_FM_dial_gets_wide_FM(self) -> None:
        assert bands.defaults_for(99_500_000)[0] == "wbfm"

    def test_shortwave_between_the_broadcast_bands_still_gets_AM(self) -> None:
        assert bands.defaults_for(8_000_000)[0] == "am"

    def test_the_top_of_the_range_is_covered(self) -> None:
        """1766 MHz is the last frequency the tuner reaches, and an off-by-one in the
        fallback ladder would leave it with nothing."""
        assert bands.defaults_for(1_766_000_000)[0]

    def test_a_channel_list_section_still_yields_a_usable_step(self) -> None:
        """MURS is five fixed frequencies 2.8 MHz apart, so `step_hz` is 0 — stepping
        between them is meaningless. But the expert path can land there by typing a
        number, and a step of zero is a pair of ± buttons that do nothing. Found by the
        sweep above, which is why it sweeps rather than spot-checks."""
        section = bands.containing(152_000_000)
        assert section is not None and section.step_hz == 0  # it really has none

        mode, step, _ = bands.defaults_for(152_000_000)

        assert mode == "fm" and step == 12_500


class TestRefusingASweepInWordsRatherThanASchema:
    """Measured on the box: asking to sweep 9.9-10.1 MHz returned a bare 422 with a
    FastAPI validation blob, because the route's `Query` bound rejected before anything
    could explain itself. The sentence existed — in the sidecar — and was unreachable.

    This is the one surface an owner with no terminal has (CLAUDE.md #10), and the
    distinction it has to carry is the least obvious one on this hardware: shortwave is
    perfectly listenable and cannot be swept.
    """

    def test_shortwave_is_refused_with_a_reason_and_a_way_forward(self) -> None:
        from jbrain.sdr.tuner import sweepable

        refusal = sweepable(10.0)

        assert refusal is not None
        assert "cannot go below" in refusal
        assert "still listen" in refusal  # ...says what DOES work down there

    def test_a_frequency_the_radio_cannot_reach_says_that_instead(self) -> None:
        """A different failure needing different words: 2000 MHz is not 'listen instead',
        it is 'this radio does not go there'."""
        from jbrain.sdr.tuner import sweepable

        refusal = sweepable(2000.0)

        assert refusal is not None and "above what this radio reaches" in refusal

    def test_a_sweepable_frequency_is_waved_through(self) -> None:
        from jbrain.sdr.tuner import sweepable

        assert sweepable(144.0) is None
        assert sweepable(24.0) is None  # exactly at the handover, where the tuner starts
