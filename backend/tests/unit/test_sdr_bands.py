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

    def test_sections_above_the_first_nyquist_zone_are_flagged_mirrored(self) -> None:
        """Above 14.4 MHz a signal aliases to 28.8 − f and arrives with images of the
        strong stations below it, sidebands swapped. Real, but not something to hand
        someone without saying so."""
        assert (
            bands.Section(
                id="t",
                band="B",
                name="N",
                start_hz=21_000_000,
                stop_hz=21_450_000,
                mode="usb",
                step_hz=100,
                channel_hz=0,
            ).mirrored
            is True
        )
        twenty = bands.by_id("20m")
        assert twenty is not None
        # 14.350 clears the 14.4 MHz boundary deliberately — it is the last band that does.
        assert twenty.mirrored is False

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
        assert wwv.mirrored is False

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
