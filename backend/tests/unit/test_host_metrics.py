"""The /proc/meminfo parser behind the drawer's memory meter, and the amdgpu
busy-percent read behind the top bar's GPU vitals stream."""

from pathlib import Path

import pytest

from jbrain.host_metrics import read_gpu_busy_percent, read_memory_gb, read_page_cache_gb

# The shape that killed the box on 2026-08-19, in miniature: a big MemAvailable sitting on a
# tiny MemFree, because ~39 GiB of page cache held a second copy of the model weights that
# `--no-mmap` left behind. The budget must read the FREE number, not the available one.
_SAMPLE = """MemTotal:       131923456 kB
MemFree:          1234567 kB
MemAvailable:    65961728 kB
Buffers:           123456 kB
SReclaimable:      262144 kB
"""


def test_used_counts_page_cache_because_reclaiming_it_livelocks_this_box(tmp_path: Path) -> None:
    """`used = MemTotal - MemFree - SReclaimable`, NOT `MemTotal - MemAvailable`.

    The old formula reported 62.9 GiB used against this sample. The box really had 1.2 GiB
    free; the other 63 GiB the kernel called "available" was page cache it could only reclaim
    by fighting the iGPU for pinned GTT — the reclaim storm that livelocked the host for seven
    hours. Admitting a model against 62.9 GiB of imaginary headroom is the bug."""
    p = tmp_path / "meminfo"
    p.write_text(_SAMPLE)
    result = read_memory_gb(str(p))
    assert result is not None
    total, used = result
    # 131923456 kB / 1048576 ≈ 125.8 GiB. Free = MemFree + SReclaimable ≈ 1.18 + 0.25 GiB,
    # so used ≈ 124.4 — the box is nearly full, which is the truth.
    assert total == pytest.approx(125.8, abs=0.1)
    assert used == pytest.approx(124.4, abs=0.2)
    assert used > 100.0, "MemAvailable must not be what this reports"


def test_slab_that_is_genuinely_cheap_to_reclaim_is_still_credited(tmp_path: Path) -> None:
    """SReclaimable is dentry/inode cache — giving it back is cheap and is not part of the
    GTT-pressure failure mode, so it counts as free. Only the page cache moved."""
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1048576 kB\nMemFree: 262144 kB\nSReclaimable: 262144 kB\n")
    result = read_memory_gb(str(p))
    assert result is not None
    assert result[1] == pytest.approx(0.5, abs=0.01)  # 1 GiB total, 0.5 GiB free


def test_a_kernel_without_sreclaimable_still_reports(tmp_path: Path) -> None:
    """Older/uncommon kernels omit it. Reporting a slightly pessimistic number beats
    returning None and leaving the budget with nothing to decide on."""
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 1048576 kB\nMemFree: 524288 kB\n")
    result = read_memory_gb(str(p))
    assert result is not None and result[1] == pytest.approx(0.5, abs=0.01)


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_memory_gb(str(tmp_path / "absent")) is None


def test_missing_fields_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "meminfo"
    p.write_text("MemAvailable: 5 kB\n")  # no MemTotal/MemFree
    assert read_memory_gb(str(p)) is None


def test_malformed_value_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: not-a-number kB\nMemFree: 1 kB\n")
    assert read_memory_gb(str(p)) is None


# --- amdgpu busy percent (the top bar's GPU reading) ------------------------


def _card(drm: Path, name: str, value: str) -> None:
    device = drm / name / "device"
    device.mkdir(parents=True)
    (device / "gpu_busy_percent").write_text(value)


def test_reads_the_card_busy_percent(tmp_path: Path) -> None:
    _card(tmp_path, "card0", "73\n")
    assert read_gpu_busy_percent(tmp_path) == 73.0


def test_takes_the_busiest_card(tmp_path: Path) -> None:
    # The iGPU's card index isn't fixed, so the read spans every card and reports
    # the busiest rather than assuming card0 is the one doing the work.
    _card(tmp_path, "card0", "4")
    _card(tmp_path, "card1", "91")
    assert read_gpu_busy_percent(tmp_path) == 91.0


def test_no_gpu_returns_none_not_zero(tmp_path: Path) -> None:
    # None is the "no telemetry here" signal — the top bar drops the reading rather
    # than claiming an idle GPU it can't actually see.
    assert read_gpu_busy_percent(tmp_path) is None


def test_absent_drm_directory_returns_none(tmp_path: Path) -> None:
    assert read_gpu_busy_percent(tmp_path / "absent") is None


def test_malformed_card_is_skipped(tmp_path: Path) -> None:
    _card(tmp_path, "card0", "not-a-number")
    _card(tmp_path, "card1", "58")
    assert read_gpu_busy_percent(tmp_path) == 58.0


def test_all_cards_malformed_returns_none(tmp_path: Path) -> None:
    _card(tmp_path, "card0", "")
    assert read_gpu_busy_percent(tmp_path) is None


def test_page_cache_is_reported_separately_from_used(tmp_path: Path) -> None:
    """`read_memory_gb` counts page cache as used and must keep doing so — that conservatism is
    what the 2026-08-19 livelock bought. But the settings meter drew `used` minus each resident
    model's catalog estimate and labelled the whole remainder "System + other containers (OS,
    database, on-box services)", so with `--no-mmap` leaving a second copy of every model's
    weights in page cache, the segment naming the OS was mostly model weights. Reporting the
    figure separately is what lets the meter name it."""
    p = tmp_path / "meminfo"
    p.write_text(_SAMPLE + "Cached:          41943040 kB\n")
    assert read_page_cache_gb(str(p)) == 40.0
    # Still counted inside `used` — the budget's view is unchanged by this addition.
    mem = read_memory_gb(str(p))
    assert mem is not None
    total, used = mem
    assert used > 40.0


def test_page_cache_is_none_when_absent(tmp_path: Path) -> None:
    # A kernel or container without a `Cached` line reports nothing rather than 0.0 — the meter
    # then keeps its old undifferentiated segment instead of drawing an empty cache block.
    p = tmp_path / "meminfo"
    p.write_text(_SAMPLE)
    assert read_page_cache_gb(str(p)) is None
    assert read_page_cache_gb(str(tmp_path / "absent")) is None
