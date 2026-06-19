"""The /proc/meminfo parser behind the drawer's memory meter."""

from pathlib import Path

import pytest

from jbrain.host_metrics import read_memory_gb

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
