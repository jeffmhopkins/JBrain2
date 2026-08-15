"""The /proc/meminfo parser behind the drawer's memory meter, and the amdgpu
busy-percent read behind the top bar's GPU vitals stream."""

from pathlib import Path

import pytest

from jbrain.host_metrics import read_gpu_busy_percent, read_memory_gb

_SAMPLE = """MemTotal:       131923456 kB
MemFree:          1234567 kB
MemAvailable:    65961728 kB
Buffers:           123456 kB
"""


def test_parses_total_and_used(tmp_path: Path) -> None:
    p = tmp_path / "meminfo"
    p.write_text(_SAMPLE)
    result = read_memory_gb(str(p))
    assert result is not None
    total, used = result
    # 131923456 kB / 1048576 ≈ 125.8 GiB; used = (total - available) ≈ 62.9 GiB.
    assert total == pytest.approx(125.8, abs=0.1)
    assert used == pytest.approx(62.9, abs=0.1)


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert read_memory_gb(str(tmp_path / "absent")) is None


def test_missing_fields_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "meminfo"
    p.write_text("MemFree: 5 kB\n")  # no MemTotal/MemAvailable
    assert read_memory_gb(str(p)) is None


def test_malformed_value_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: not-a-number kB\nMemAvailable: 1 kB\n")
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
