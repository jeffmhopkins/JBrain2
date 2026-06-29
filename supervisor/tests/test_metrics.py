"""Host metrics parsing and the /metrics route."""

from pathlib import Path

from fastapi.testclient import TestClient

from supervisor import host_metrics
from tests.conftest import AUTH

MEMINFO = """MemTotal:        4030000 kB
MemFree:          512000 kB
MemAvailable:    1500000 kB
SwapTotal:       2097152 kB
SwapFree:        2000000 kB
"""


def _drm_with_busy(root: Path, by_card: dict[str, str]) -> Path:
    """Build a fake /sys/class/drm tree: {card_name: gpu_busy_percent text}."""
    for card, value in by_card.items():
        device = root / card / "device"
        device.mkdir(parents=True)
        (device / "gpu_busy_percent").write_text(value)
    return root


def _hwmon_with_fans(root: Path, by_chip: dict[str, dict[str, str]]) -> Path:
    """Build a fake /sys/class/hwmon tree: {hwmonN: {filename: text}}.

    Filenames are the hwmon leaf names, e.g. "name", "fan1_input", "fan1_label".
    """
    for chip, files in by_chip.items():
        chip_dir = root / chip
        chip_dir.mkdir(parents=True)
        for name, text in files.items():
            (chip_dir / name).write_text(text)
    return root


def test_read_host_metrics_parses_proc(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(MEMINFO)
    (tmp_path / "loadavg").write_text("0.42 0.30 0.18 1/123 4567\n")
    (tmp_path / "uptime").write_text("86400.55 170000.00\n")
    drm = _drm_with_busy(tmp_path / "drm", {"card0": "37\n"})
    hwmon = _hwmon_with_fans(
        tmp_path / "hwmon",
        {
            "hwmon0": {
                "name": "amdgpu\n",
                "fan1_input": "2100\n",
                "power1_average": "14001000\n",
            }
        },
    )

    m = host_metrics.read_host_metrics(
        proc=tmp_path, disk_path="/", drm=drm, hwmon=hwmon
    )

    assert m.mem_total_bytes == 4030000 * 1024
    assert m.mem_available_bytes == 1500000 * 1024
    assert m.swap_total_bytes == 2097152 * 1024
    assert m.load_1m == 0.42
    assert m.load_15m == 0.18
    assert m.uptime_seconds == 86400
    assert m.disk_total_bytes > 0
    assert m.gpu_busy_percent == 37.0
    assert m.fan_rpm == {"amdgpu fan1": 2100}
    assert m.apu_power_w == 14.001


def test_read_apu_power_prefers_average_over_input(tmp_path: Path) -> None:
    hwmon = _hwmon_with_fans(
        tmp_path,
        {
            "hwmon0": {"name": "k10temp\n"},  # a non-amdgpu chip is skipped
            "hwmon1": {
                "name": "amdgpu\n",
                "power1_average": "14001000\n",
                "power1_input": "9000000\n",
            },
        },
    )
    # µW -> W, from the amdgpu chip, preferring the smoothed average.
    assert host_metrics.read_apu_power_w(hwmon) == 14.001


def test_read_apu_power_falls_back_to_input(tmp_path: Path) -> None:
    hwmon = _hwmon_with_fans(
        tmp_path, {"hwmon0": {"name": "amdgpu\n", "power1_input": "5500000\n"}}
    )
    assert host_metrics.read_apu_power_w(hwmon) == 5.5


def test_read_apu_power_is_none_without_amdgpu(tmp_path: Path) -> None:
    # No amdgpu power attribute (non-AMD / no /sys): None, never a fake 0.
    _hwmon_with_fans(tmp_path, {"hwmon0": {"name": "k10temp\n"}})
    assert host_metrics.read_apu_power_w(tmp_path) is None


def test_read_fan_rpm_prefers_label_and_keeps_zero(tmp_path: Path) -> None:
    hwmon = _hwmon_with_fans(
        tmp_path,
        {
            "hwmon0": {
                "name": "k10temp\n",
                "fan1_input": "0\n",
                "fan1_label": "CPU Fan\n",
            },
            "hwmon1": {"name": "amdgpu\n", "fan1_input": "1850\n"},
        },
    )
    # A labelled fan uses its label; an unlabelled one falls back to chip + index.
    # A 0 reading is a stopped fan, not absent telemetry, so it is kept.
    assert host_metrics.read_fan_rpm(hwmon) == {"CPU Fan": 0, "amdgpu fan1": 1850}


def test_read_fan_rpm_disambiguates_duplicate_labels(tmp_path: Path) -> None:
    hwmon = _hwmon_with_fans(
        tmp_path,
        {
            "hwmon0": {"name": "ec\n", "fan1_input": "1200\n", "fan1_label": "Fan\n"},
            "hwmon1": {"name": "ec\n", "fan1_input": "1300\n", "fan1_label": "Fan\n"},
        },
    )
    assert host_metrics.read_fan_rpm(hwmon) == {"Fan": 1200, "Fan (2)": 1300}


def test_read_fan_rpm_is_none_without_telemetry(tmp_path: Path) -> None:
    # No hwmon fan inputs (a VM / fanless box / no /sys): None, never an empty dict.
    assert host_metrics.read_fan_rpm(tmp_path) is None


def test_read_gpu_busy_takes_the_busiest_card(tmp_path: Path) -> None:
    drm = _drm_with_busy(tmp_path, {"card0": "12\n", "card1": "88\n"})
    assert host_metrics.read_gpu_busy_percent(drm) == 88.0


def test_read_gpu_busy_is_none_without_telemetry(tmp_path: Path) -> None:
    # No amdgpu attribute (non-AMD box / /sys not exposed): None, never a fake 0.
    assert host_metrics.read_gpu_busy_percent(tmp_path) is None


def test_metrics_route(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        host_metrics,
        "read_host_metrics",
        lambda: host_metrics.HostMetrics(
            mem_total_bytes=4 << 30,
            mem_available_bytes=1 << 30,
            swap_total_bytes=0,
            swap_free_bytes=0,
            disk_total_bytes=40 << 30,
            disk_free_bytes=25 << 30,
            load_1m=0.5,
            load_5m=0.4,
            load_15m=0.3,
            uptime_seconds=12345,
            gpu_busy_percent=42.0,
            fan_rpm={"CPU Fan": 2100},
            apu_power_w=14.0,
        ),
    )
    assert client.get("/metrics").status_code == 401

    body = client.get("/metrics", headers=AUTH).json()
    assert body["mem_total_bytes"] == 4 << 30
    assert body["disk_free_bytes"] == 25 << 30
    assert body["gpu_busy_percent"] == 42.0
    assert body["fan_rpm"] == {"CPU Fan": 2100}
    assert body["apu_power_w"] == 14.0
    assert body["containers"]
    assert body["containers"][0]["mem_bytes"] == 100 << 20


def test_processes_route(client: TestClient) -> None:
    assert client.get("/processes").status_code == 401

    body = client.get("/processes", headers=AUTH).json()
    assert body["processes"]
    first = body["processes"][0]
    assert first["rss_bytes"] == 50 << 20
    assert first["pid"] == 1000
    assert "--serve" in first["command"]
