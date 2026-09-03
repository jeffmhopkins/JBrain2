"""Reducing a band sweep.

What is pinned here is mostly about NOT LYING when the data is thin. A sweep reduction
fails plausibly: an empty band and a broken parse both come back as "nothing busy", a
per-bin artifact reads as a station, and a signal spread across three bins reads as
three signals. Every one of those looks like a normal answer on screen.
"""

from __future__ import annotations

import pytest

from jbrain.sdr.sweep import (
    Bin,
    Reduced,
    channels,
    reduce_csv,
    steady_channels,
    waterfall_png,
)


def _row(when: str, low: int, high: int, step: int, *values: float) -> str:
    return f"2026-09-03, {when}, {low}, {high}, {step}, 12, " + ", ".join(
        f"{v:.2f}" for v in values
    )


def _quiet(when: str, *, floor: float = -98.0, bins: int = 4) -> str:
    return _row(when, 144_000_000, 144_020_000, 5_000, *[floor] * bins)


class TestReadingTheCsv:
    def test_a_sweep_of_nothing_is_a_sweep_of_nothing(self) -> None:
        reduced = reduce_csv("\n".join(_quiet(f"15:00:0{i}") for i in range(5)))

        assert reduced.rows == 5
        assert reduced.bins == 4
        assert reduced.busy == []
        assert reduced.floor_db == pytest.approx(-98.0)

    def test_a_carrier_shows_up_as_occupancy_not_just_a_peak(self) -> None:
        """A one-off burst and a channel busy half the window have the SAME peak. Only
        one of them is a busy channel, and the peak cannot tell them apart."""
        rows = [_quiet(f"15:00:0{i}") for i in range(10)]
        for i in (2, 3, 4, 5, 6):  # busy 5 intervals in 10, in the second bin
            rows[i] = _row(
                f"15:00:0{i}", 144_000_000, 144_020_000, 5_000, -98.0, -70.0, -98.0, -98.0
            )

        reduced = reduce_csv("\n".join(rows))

        assert [b.hz for b in reduced.busy] == [144_005_000]
        assert reduced.busy[0].occupancy == pytest.approx(0.5)
        assert reduced.busy[0].peak_db == pytest.approx(-70.0)

    def test_the_floor_is_per_bin_so_a_spur_does_not_become_a_station(self) -> None:
        """The tuner's DC spike sits at the centre of every retune block, and spurs sit
        wherever the hardware puts them. A single global floor reports each of them as a
        station transmitting 100% of the time; a per-bin floor absorbs them, because a
        constant offset IS that bin's floor."""
        rows = [
            _row(f"15:00:0{i}", 144_000_000, 144_020_000, 5_000, -98.0, -60.0, -98.0, -98.0)
            for i in range(8)
        ]

        reduced = reduce_csv("\n".join(rows))

        # The steady -60 dB bin is 38 dB over the others and never varies: it is the
        # noise floor of a bin that has a spur in it, not a signal.
        assert reduced.busy == []

    def test_a_wide_sweep_stitches_its_retune_blocks_by_frequency(self) -> None:
        """A span wider than the tuner is swept in hops, and rtl_power emits one row per
        BLOCK. Assuming they arrive in order gives a grid with the bands shuffled."""
        text = "\n".join(
            [
                _row("15:00:00", 144_010_000, 144_020_000, 5_000, -98.0, -98.0),
                _row("15:00:00", 144_000_000, 144_010_000, 5_000, -98.0, -70.0),
                _row("15:00:01", 144_010_000, 144_020_000, 5_000, -98.0, -98.0),
                _row("15:00:01", 144_000_000, 144_010_000, 5_000, -98.0, -70.0),
            ]
        )

        reduced = reduce_csv(text)

        assert reduced.start_hz == 144_000_000
        assert reduced.stop_hz == 144_020_000
        assert reduced.bins == 4

    def test_a_truncated_final_row_costs_an_interval_not_the_sweep(self) -> None:
        # rtl_power writes a partial row when its exit timer fires mid-block. That is a
        # real measurement of fewer bins, not a parse error.
        text = "\n".join([_quiet("15:00:00"), _quiet("15:00:01"), "2026-09-03, 15:00:02, 144"])

        reduced = reduce_csv(text)

        assert reduced.rows == 2

    def test_garbage_in_the_middle_does_not_lose_the_sweep(self) -> None:
        # This runs over a file a radio wrote. Losing one interval must not lose the run.
        text = "\n".join([_quiet("15:00:00"), "not a row at all,,,", _quiet("15:00:01")])

        assert reduce_csv(text).rows == 2

    def test_an_empty_file_reduces_to_nothing_rather_than_raising(self) -> None:
        reduced = reduce_csv("")

        assert reduced.rows == 0 and reduced.busy == []


class TestChannels:
    def test_one_transmission_across_three_bins_is_one_channel(self) -> None:
        """A 16 kHz FM signal in a 5 kHz sweep lights several adjacent bins. Reported as
        bins it reads as three stations a few kHz apart, which is not a thing that
        happens on a channelized band."""
        reduced = Reduced(
            rows=10,
            start_hz=146_930_000,
            stop_hz=146_950_000,
            bin_hz=5_000,
            floor_db=-98.0,
            busy=[
                Bin(hz=146_935_000, floor_db=-98.0, peak_db=-80.0, occupancy=0.3),
                Bin(hz=146_940_000, floor_db=-98.0, peak_db=-71.0, occupancy=0.5),
                Bin(hz=146_945_000, floor_db=-98.0, peak_db=-82.0, occupancy=0.3),
            ],
        )

        folded = channels(reduced, 15_000)

        assert len(folded) == 1
        # The centre bin stands for the channel: a transmission's peak is at its centre
        # and the skirts are the same signal seen worse.
        assert folded[0].peak_db == pytest.approx(-71.0)
        assert folded[0].occupancy == pytest.approx(0.5)

    def test_two_real_channels_stay_two(self) -> None:
        reduced = Reduced(
            rows=10,
            start_hz=146_000_000,
            stop_hz=147_000_000,
            bin_hz=5_000,
            floor_db=-98.0,
            busy=[
                Bin(hz=146_940_000, floor_db=-98.0, peak_db=-71.0, occupancy=0.5),
                Bin(hz=146_520_000, floor_db=-98.0, peak_db=-77.0, occupancy=0.2),
            ],
        )

        assert len(channels(reduced, 15_000)) == 2

    def test_spacing_is_the_caller_s_because_band_plans_differ(self) -> None:
        # Parts of the US use 20 kHz on 2m, and 12.5 kHz narrowband exists. A hardcoded
        # 15 would fold two real channels into one wherever that is not the local plan.
        reduced = Reduced(
            rows=4,
            start_hz=145_000_000,
            stop_hz=145_100_000,
            bin_hz=5_000,
            floor_db=-98.0,
            busy=[
                Bin(hz=145_010_000, floor_db=-98.0, peak_db=-70.0, occupancy=0.5),
                Bin(hz=145_030_000, floor_db=-98.0, peak_db=-70.0, occupancy=0.5),
            ],
        )

        assert len(channels(reduced, 20_000)) == 2
        assert len(channels(reduced, 0)) == 2  # no spacing means no folding


class TestTheImage:
    def test_it_draws_a_png_the_size_of_the_grid(self) -> None:
        reduced = reduce_csv("\n".join(_quiet(f"15:00:0{i}") for i in range(6)))

        png = waterfall_png(reduced)

        assert png.startswith(b"\x89PNG")
        # 4 bins wide, 6 intervals tall — the grid, not a fixed canvas.
        assert png[16:24] == (4).to_bytes(4, "big") + (6).to_bytes(4, "big")

    def test_an_empty_sweep_draws_nothing_rather_than_a_blank_canvas(self) -> None:
        # A 1x1 black square is an image that says the band was quiet. It isn't; it says
        # nothing was measured.
        assert waterfall_png(reduce_csv("")) == b""

    def test_the_scale_follows_the_data_the_way_a_human_drags_the_slider(self) -> None:
        """Dragging contrast is not a sensitivity control — it cannot pull signal out of
        noise, because the signal was always in the numbers. It finds the floor and
        stretches the palette just above it, which is a percentile."""
        rows = [_quiet(f"15:00:0{i}", floor=-98.0) for i in range(9)]
        rows[4] = _row("15:00:04", 144_000_000, 144_020_000, 5_000, -98.0, -40.0, -98.0, -98.0)

        auto = waterfall_png(reduce_csv("\n".join(rows)))
        # Forcing the range to sit entirely above the data makes every pixel the floor
        # colour — a different image, which is what proves the default is data-driven.
        forced = waterfall_png(reduce_csv("\n".join(rows)), floor_db=0.0, ceil_db=10.0)

        assert auto != forced


class TestTheGridProblem:
    """Why folding is by adjacency and not by snapping to a grid.

    `round(hz / spacing) * spacing` assumes the channel grid is anchored at 0 Hz. Real
    band plans are not — and two bins 5 kHz apart can straddle a 15 kHz boundary, which
    splits one transmission into two stations. That is the exact failure folding exists
    to prevent, so it would have been a silent own-goal."""

    def _busy(self, *pairs: tuple[int, float]) -> Reduced:
        return Reduced(
            rows=8,
            start_hz=144_000_000,
            stop_hz=144_100_000,
            bin_hz=5_000,
            floor_db=-98.0,
            busy=[Bin(hz=hz, floor_db=-98.0, peak_db=db, occupancy=0.25) for hz, db in pairs],
        )

    def test_two_bins_straddling_a_grid_boundary_are_still_one_signal(self) -> None:
        # 144.005 and 144.010 land either side of a multiple of 15 kHz. Snapping puts
        # them in different channels; they are 5 kHz apart and obviously one signal.
        folded = channels(self._busy((144_005_000, -70.0), (144_010_000, -74.0)), 15_000)

        assert len(folded) == 1
        assert folded[0].hz == 144_005_000  # the peak names it

    def test_two_bins_a_full_channel_apart_stay_two(self) -> None:
        # The other half of the rule: adjacent CHANNELS must not merge just because they
        # are the closest things in the sweep.
        folded = channels(self._busy((144_010_000, -70.0), (144_030_000, -70.0)), 20_000)

        assert len(folded) == 2

    def test_occupancy_survives_a_signal_drifting_between_bins(self) -> None:
        """A transmission that wanders a bin between intervals is present the whole
        time even though no single bin was — so occupancy is the max across the
        cluster, not the peak bin's own."""
        drifting = Reduced(
            rows=8,
            start_hz=144_000_000,
            stop_hz=144_100_000,
            bin_hz=5_000,
            floor_db=-98.0,
            busy=[
                Bin(hz=144_005_000, floor_db=-98.0, peak_db=-70.0, occupancy=0.25),
                Bin(hz=144_010_000, floor_db=-98.0, peak_db=-74.0, occupancy=0.5),
            ],
        )

        (folded,) = channels(drifting, 15_000)

        assert folded.peak_db == pytest.approx(-70.0)  # named by the loudest
        assert folded.occupancy == pytest.approx(0.5)  # but present as often as either


def test_the_floor_is_a_percentile_so_one_loud_burst_does_not_mask_the_rest() -> None:
    """Why a percentile and not a mean.

    A mean is dragged upward by exactly the signals being hunted, and the threshold
    rides up with it — so a bin holding one very strong burst reports ONLY that burst
    and hides the weaker traffic underneath. The channel then reads as almost idle when
    it was busy three intervals in eight."""
    # One -30 dB burst, two weaker -88 dB transmissions, and silence at -98.
    levels = [-30.0, -88.0, -88.0, -98.0, -98.0, -98.0, -98.0, -98.0]
    text = "\n".join(
        _row(f"15:00:0{i}", 144_000_000, 144_005_000, 5_000, db) for i, db in enumerate(levels)
    )

    reduced = reduce_csv(text)

    # The floor is the quiet level, so all three transmissions clear it. A mean floor of
    # about -87 would put the threshold above the -88s and find only the loud one.
    assert reduced.busy[0].occupancy == pytest.approx(3 / 8)


class TestTheSteadyCarrierProblem:
    """The failure a real 2m sweep of the owner's box produced on 2026-09-03.

    Six repeater outputs sat lit on the waterfall for the whole window — 146.660,
    146.720, 146.910, 147.030, 147.210 — and the detector returned `busy: []` and
    `steady: []`. Occupancy could never have found them (a carrier held the whole window
    IS its own floor, so it is 0% over that floor), which is what `steady` exists for.
    `steady` missed them because it compared each bin to the SWEEP's median floor, and
    rtl_power's two retune halves had floors 1.76x apart: one median across two
    unrelated populations sits above everything in the quiet half.
    """

    def _two_halves(self, *, carrier_at: int | None) -> str:
        """A sweep whose upper half is 18 dB noisier than its lower — measured, not
        invented: this box's 144-148 run read 84.5 vs 48.1 mean brightness across the
        retune seam."""
        rows = []
        for i in range(8):
            low = [-98.0] * 40
            if carrier_at is not None:
                # 20 dB over its own neighbourhood, and still BELOW the noisy half's
                # floor: the numbers that make the two comparisons disagree.
                low[carrier_at] = -78.0
            rows.append(_row(f"15:00:0{i}", 144_000_000, 145_000_000, 25_000, *low))
            rows.append(_row(f"15:00:0{i}", 145_000_000, 146_000_000, 25_000, *([-80.0] * 40)))
        return "\n".join(rows)

    def test_a_carrier_in_the_quiet_half_is_found_despite_the_noisy_half(self) -> None:
        reduced = reduce_csv(self._two_halves(carrier_at=20))

        # Under the old sweep-wide comparison this bin sat 10 dB BELOW the bar (median
        # -80, plus 12) while sitting 28 dB above its own neighbours. It is a carrier.
        assert [b.hz for b in reduced.steady] == [144_500_000]
        # ...and occupancy still cannot see it, which is the whole reason `steady` exists.
        assert reduced.busy == []

    def test_the_noisy_half_is_not_reported_as_one_long_carrier(self) -> None:
        """The mirror failure, and the reason the bar is local rather than just lower:
        drop the threshold enough to catch the quiet half's carrier against a global
        median and every bin of the noisy half clears it too."""
        reduced = reduce_csv(self._two_halves(carrier_at=None))

        assert reduced.steady == []

    def test_one_carrier_across_three_bins_is_one_line_not_three(self) -> None:
        """A held carrier is as many bins wide as a keyed-up one. A list read by eye
        must not show one repeater three times."""
        rows = []
        for i in range(8):
            values = [-98.0] * 200
            values[99], values[100], values[101] = -70.0, -68.0, -70.0
            rows.append(_row(f"15:00:0{i}", 144_000_000, 145_000_000, 5_000, *values))
        reduced = reduce_csv("\n".join(rows))

        assert len(reduced.steady) == 3
        # One signal, standing at its strongest bin.
        assert [b.hz for b in steady_channels(reduced, 15_000)] == [144_500_000]


class TestSayingWhatWasNotMeasured:
    """Silence is only evidence where the receiver was listening.

    This box's 144-148 sweep left 145.872-146.206 unmeasured — a 342 kHz hole at the
    retune seam, sitting straight across live repeater channels. Reported as "nothing
    busy there" it reads as a quiet band, which is the opposite of what it means."""

    def test_a_gap_between_retune_blocks_is_reported_not_hidden(self) -> None:
        text = "\n".join(
            [
                _row("15:00:00", 144_000_000, 144_010_000, 5_000, -98.0, -98.0),
                _row("15:00:00", 144_100_000, 144_110_000, 5_000, -98.0, -98.0),
                _row("15:00:01", 144_000_000, 144_010_000, 5_000, -98.0, -98.0),
                _row("15:00:01", 144_100_000, 144_110_000, 5_000, -98.0, -98.0),
            ]
        )

        reduced = reduce_csv(text)

        # The first block's bins are at 144.000 and 144.005, so coverage ends at 144.010.
        assert reduced.uncovered == [(144_010_000, 144_100_000)]

    def test_a_sweep_with_no_holes_reports_no_holes(self) -> None:
        reduced = reduce_csv("\n".join(_quiet(f"15:00:0{i}") for i in range(5)))

        assert reduced.uncovered == []

    def test_a_bin_of_nothing_is_a_hole_not_a_station(self) -> None:
        """rtl_power can hand back a non-finite bin. A floor of -inf sits under every
        sample in the column, so left in it reports 100% occupancy at a peak of -inf —
        the loudest signal on the band, made of no measurement at all."""
        rows = [
            _row(f"15:00:0{i}", 144_000_000, 144_020_000, 5_000, -98.0, float("-inf"), -98.0, -98.0)
            for i in range(6)
        ]

        reduced = reduce_csv("\n".join(rows))

        assert reduced.busy == []
        assert reduced.steady == []
        assert reduced.uncovered == [(144_005_000, 144_010_000)]

    def test_a_bin_that_lost_some_intervals_is_still_a_measured_bin(self) -> None:
        """Losing three of eight readings is not losing the bin.

        Treating a column with ANY non-finite value as unmeasured throws away a real
        measurement of a real frequency — and would quietly blank whole bands, since a
        dropped interval is a normal thing for a radio to do."""
        rows = [
            _row(
                f"15:00:0{i}",
                144_000_000,
                144_020_000,
                5_000,
                -98.0,
                (float("nan") if i < 3 else -70.0),
                -98.0,
                -98.0,
            )
            for i in range(8)
        ]

        reduced = reduce_csv("\n".join(rows))

        assert reduced.uncovered == []
        # Occupancy is over the readings that EXIST: five of five, not five of eight.
        assert [(b.hz, b.occupancy) for b in reduced.busy] == []
        assert [b.hz for b in reduced.steady] == [144_005_000]

    def test_overlapping_retune_blocks_are_not_a_hole(self) -> None:
        """rtl_power's blocks can overlap where it crops their edges. Walking coverage
        with a plain running edge instead of a high-water mark makes the second block's
        lower half look like it comes BEFORE where the first one ended, and invents a
        gap out of the overlap."""
        text = "\n".join(
            [
                _row("15:00:00", 144_000_000, 144_020_000, 5_000, -98.0, -98.0, -98.0, -98.0),
                _row("15:00:00", 144_010_000, 144_030_000, 5_000, -98.0, -98.0, -98.0, -98.0),
                _row("15:00:01", 144_000_000, 144_020_000, 5_000, -98.0, -98.0, -98.0, -98.0),
                _row("15:00:01", 144_010_000, 144_030_000, 5_000, -98.0, -98.0, -98.0, -98.0),
            ]
        )

        assert reduce_csv(text).uncovered == []

    def test_a_block_sitting_inside_another_does_not_invent_a_hole(self) -> None:
        """Blocks are keyed by the header the radio wrote, and nothing guarantees they
        tile the band exactly once — this parser's whole stance is that a radio wrote
        the file. Sorted by (low, high), a narrow block nested in a wide one lands
        BETWEEN the wide one's bins and the next block's, so walking coverage in the
        order the blocks arrive steps backwards at the seam and reports the difference
        as unmeasured spectrum. Every one of those bins was measured."""
        rows = []
        for i in range(4):
            rows += [
                _row(f"15:00:0{i}", 144_000_000, 144_030_000, 5_000, *([-98.0] * 6)),
                _row(f"15:00:0{i}", 144_010_000, 144_020_000, 5_000, -98.0, -98.0),
                _row(f"15:00:0{i}", 144_030_000, 144_040_000, 5_000, -98.0, -98.0),
            ]

        assert reduce_csv("\n".join(rows)).uncovered == []


class TestTheBaselineWindow:
    def test_a_coarse_sweep_still_gets_a_window_wider_than_the_signal(self) -> None:
        """400 kHz of neighbours is four bins at the route's coarsest resolution, and a
        signal three bins wide is then most of its own baseline — it becomes the median
        it is measured against and hides itself. The floor is a minimum bin count for
        exactly that."""
        rows = []
        for i in range(6):
            values = [-98.0] * 40
            values[19] = values[20] = values[21] = -70.0
            rows.append(_row(f"15:00:0{i}", 144_000_000, 148_000_000, 100_000, *values))

        reduced = reduce_csv("\n".join(rows))

        assert [b.hz for b in reduced.steady] == [
            145_900_000,
            146_000_000,
            146_100_000,
        ]

    def test_the_loudest_carrier_is_listed_first(self) -> None:
        """The route truncates this list, so the order decides what a reader sees."""
        rows = []
        for i in range(6):
            values = [-98.0] * 200
            values[40], values[120] = -80.0, -60.0
            rows.append(_row(f"15:00:0{i}", 144_000_000, 145_000_000, 5_000, *values))

        reduced = reduce_csv("\n".join(rows))

        assert [b.peak_db for b in reduced.steady] == [-60.0, -80.0]

    def test_one_short_row_does_not_delete_bins_from_every_other_row(self) -> None:
        """rtl_power's exit timer cuts a row off mid-block. Trimming the block to that
        row's width throws away the bins above it for the WHOLE window — 149 good
        readings discarded because the 150th stopped early — and the band above goes
        dark with nothing saying why. MEASURED on this box: a 342 kHz hole mid-band,
        exactly the shape of a chopped block tail."""
        rows = [
            _row(f"15:00:0{i}", 144_000_000, 144_020_000, 5_000, -98.0, -98.0, -98.0, -98.0)
            for i in range(5)
        ]
        rows.append(_row("15:00:05", 144_000_000, 144_020_000, 5_000, -98.0, -98.0))

        reduced = reduce_csv("\n".join(rows))

        assert reduced.bins == 4
        assert reduced.uncovered == []
        # And the reading it never got is a MISSING reading, not a loud one: padding the
        # short row with a number instead puts a 0 dB sample in a -98 dB band, which is
        # the strongest signal on the sweep and made of nothing.
        assert reduced.busy == []
        assert reduced.steady == []
