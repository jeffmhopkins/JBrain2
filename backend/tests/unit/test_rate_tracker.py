"""The RateTracker that turns cumulative byte counters into throughput rates."""

from jbrain.ops_metrics import RateTracker


def _metrics(rx: int, tx: int, rd: int, wr: int) -> dict:
    return {
        "net": {"rx_bytes": rx, "tx_bytes": tx},
        "disk_io": {"read_bytes": rd, "write_bytes": wr},
    }


def test_first_sample_has_no_rate() -> None:
    # Nothing to diff against yet: every series is None, not zero.
    rates = RateTracker().rates(100.0, _metrics(1000, 2000, 3000, 4000))
    assert rates == {
        "net_rx_bps": None,
        "net_tx_bps": None,
        "disk_read_bps": None,
        "disk_write_bps": None,
    }


def test_second_sample_divides_delta_by_elapsed() -> None:
    t = RateTracker()
    t.rates(100.0, _metrics(1000, 2000, 3000, 4000))
    # 10s later, +5000 rx / +1000 tx / +20000 read / +2000 write.
    rates = t.rates(110.0, _metrics(6000, 3000, 23000, 6000))
    assert rates["net_rx_bps"] == 500.0
    assert rates["net_tx_bps"] == 100.0
    assert rates["disk_read_bps"] == 2000.0
    assert rates["disk_write_bps"] == 200.0


def test_counter_reset_yields_none_not_a_negative_spike() -> None:
    t = RateTracker()
    t.rates(100.0, _metrics(9000, 9000, 9000, 9000))
    # A reboot resets the counters below the prior reading: null, never negative.
    rates = t.rates(110.0, _metrics(10, 20, 30, 40))
    assert all(v is None for v in rates.values())


def test_non_positive_interval_yields_none() -> None:
    t = RateTracker()
    t.rates(100.0, _metrics(1000, 1000, 1000, 1000))
    # Same instant (clock didn't advance): no divide-by-zero, just None.
    assert all(v is None for v in t.rates(100.0, _metrics(5000, 5000, 5000, 5000)).values())


def test_missing_section_yields_none_for_that_series_only() -> None:
    t = RateTracker()
    # An older supervisor reports net but not disk_io.
    t.rates(100.0, {"net": {"rx_bytes": 1000, "tx_bytes": 2000}})
    rates = t.rates(110.0, {"net": {"rx_bytes": 2000, "tx_bytes": 2500}})
    assert rates["net_rx_bps"] == 100.0
    assert rates["net_tx_bps"] == 50.0
    assert rates["disk_read_bps"] is None
    assert rates["disk_write_bps"] is None


def test_missed_tick_widens_the_next_interval() -> None:
    t = RateTracker()
    t.rates(100.0, _metrics(0, 0, 0, 0))
    # The next *stored* sample is 60s later (a tick was skipped): the rate spreads
    # the whole delta over the real elapsed time, so it stays a true average.
    rates = t.rates(160.0, _metrics(6000, 0, 0, 0))
    assert rates["net_rx_bps"] == 100.0
