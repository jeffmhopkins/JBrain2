"""Reducing a band sweep.

What is pinned here is mostly about NOT LYING when the data is thin. A sweep reduction
fails plausibly: an empty band and a broken parse both come back as "nothing busy", a
per-bin artifact reads as a station, and a signal spread across three bins reads as
three signals. Every one of those looks like a normal answer on screen.
"""

from __future__ import annotations

import pytest

from jbrain.sdr.sweep import Bin, Reduced, channels, reduce_csv, waterfall_png


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
