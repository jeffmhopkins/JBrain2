"""Host metrics parsing and the /metrics route."""

from pathlib import Path

from fastapi.testclient import TestClient

from supervisor import host_metrics
from tests.conftest import AUTH

MEMINFO = """MemTotal:        4030000 kB
MemFree:          512000 kB
MemAvailable:    1500000 kB
Buffers:           64000 kB
Cached:           800000 kB
SwapTotal:       2097152 kB
SwapFree:        2000000 kB
Shmem:            256000 kB
AnonPages:        900000 kB
Slab:             128000 kB
SReclaimable:      96000 kB
SUnreclaim:        32000 kB
Unevictable:       10000 kB
"""


def _drm_with_busy(root: Path, by_card: dict[str, str]) -> Path:
    """Build a fake /sys/class/drm tree: {card_name: gpu_busy_percent text}."""
    for card, value in by_card.items():
        device = root / card / "device"
        device.mkdir(parents=True)
        (device / "gpu_busy_percent").write_text(value)
    return root


def _drm_with_gpu_mem(root: Path, by_card: dict[str, dict[str, str]]) -> Path:
    """Build a fake /sys/class/drm tree: {card: {mem_info_leaf: text}}."""
    for card, files in by_card.items():
        device = root / card / "device"
        device.mkdir(parents=True)
        for name, text in files.items():
            (device / name).write_text(text)
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
    # The curated breakdown carries the attribution fields present in meminfo (as
    # bytes) and omits ones that aren't there (no Mapped/Mlocked/KReclaimable line).
    assert m.mem_breakdown is not None
    assert m.mem_breakdown["Shmem"] == 256000 * 1024
    assert m.mem_breakdown["AnonPages"] == 900000 * 1024
    assert m.mem_breakdown["SUnreclaim"] == 32000 * 1024
    assert "Mapped" not in m.mem_breakdown
    # This fake DRM tree has no mem_info_* files, so GPU-memory telemetry is absent.
    assert m.gpu_mem is None


def test_read_amdgpu_mem_sums_across_cards_and_skips_malformed(tmp_path: Path) -> None:
    drm = _drm_with_gpu_mem(
        tmp_path,
        {
            "card0": {
                "mem_info_gtt_used": "10000",
                "mem_info_gtt_total": "90000",
                "mem_info_vram_used": "2000",
                "mem_info_vram_total": "8000",
            },
            # A second card exposing the same counters is summed in.
            "card1": {
                "mem_info_gtt_used": "5000",
                "mem_info_gtt_total": "90000",
                "mem_info_vram_used": "1000",
                "mem_info_vram_total": "8000",
            },
            # A card missing a field is skipped whole, never counted partially.
            "card2": {"mem_info_gtt_used": "999"},
        },
    )
    gpu = host_metrics.read_amdgpu_mem(drm)
    assert gpu is not None
    assert gpu.gtt_used_bytes == 15000
    assert gpu.gtt_total_bytes == 180000
    assert gpu.vram_used_bytes == 3000


def test_read_amdgpu_mem_is_none_without_counters(tmp_path: Path) -> None:
    # A DRM card with only gpu_busy (no mem_info_* files): None, never a zeroed
    # struct that would imply the iGPU holds nothing.
    _drm_with_busy(tmp_path, {"card0": "0\n"})
    assert host_metrics.read_amdgpu_mem(tmp_path) is None


# /proc/net/dev: two header rows (no ':', so skipped), then
# "iface: rx_bytes rx_packets ...(6 more)... tx_bytes tx_packets ...".
NET_DEV = """Inter-| Receive | Transmit
 face |bytes ... |bytes ...
    lo: 100 1 0 0 0 0 0 0 200 1 0 0 0 0 0 0
  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0
 wlan0: 500 5 0 0 0 0 0 0 700 7 0 0 0 0 0 0
docker0: 9 9 0 0 0 0 0 0 9 9 0 0 0 0 0 0
veth1a2b: 8 8 0 0 0 0 0 0 8 8 0 0 0 0 0 0
"""


def test_read_net_counters_sums_physical_only(tmp_path: Path) -> None:
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "dev").write_text(NET_DEV)
    net = host_metrics.read_net_counters(tmp_path)
    assert net is not None
    # eth0 + wlan0 only; lo, docker0, veth* excluded.
    assert net.rx_bytes == 1500
    assert net.tx_bytes == 2700


def test_read_net_counters_is_none_without_procfile(tmp_path: Path) -> None:
    assert host_metrics.read_net_counters(tmp_path) is None


# /proc/diskstats cols: major minor name reads merged sectors_read ... sectors_written
DISKSTATS = """ 259 0 nvme0n1 100 0 2048 50 200 0 4096 80 0 60 130
 259 1 nvme0n1p1 90 0 1024 40 180 0 2048 70 0 55 110
 7 0 loop0 5 0 40 2 0 0 0 0 0 1 2
 253 0 dm-0 300 0 8192 90 400 0 16384 120 0 90 210
   8 0 sda 10 0 512 5 20 0 1024 8 0 6 13
"""


def _sysblock(root: Path, names: list[str]) -> Path:
    for name in names:
        (root / name).mkdir(parents=True)
    return root


def test_read_disk_counters_sums_whole_disks_and_skips_partitions_and_virtual(
    tmp_path: Path,
) -> None:
    (tmp_path / "diskstats").write_text(DISKSTATS)
    # Whole disks are top-level /sys/block entries; the partition and dm/loop are
    # present in diskstats but must be excluded (partition not in /sys/block top
    # level; dm-/loop by prefix) so I/O isn't double-counted.
    sysblock = _sysblock(tmp_path / "block", ["nvme0n1", "dm-0", "loop0", "sda"])
    disk = host_metrics.read_disk_counters(tmp_path, sysblock)
    assert disk is not None
    # nvme0n1 (2048r/4096w sectors) + sda (512r/1024w), x512; p1/dm-0/loop0 out.
    assert disk.read_bytes == (2048 + 512) * 512
    assert disk.write_bytes == (4096 + 1024) * 512


def test_read_disk_counters_is_none_without_procfile(tmp_path: Path) -> None:
    assert host_metrics.read_disk_counters(tmp_path, tmp_path / "block") is None


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
            gpu_mem=host_metrics.GpuMem(
                gtt_used_bytes=12 << 30,
                gtt_total_bytes=100 << 30,
                vram_used_bytes=1 << 30,
                vram_total_bytes=4 << 30,
            ),
            mem_breakdown={"Shmem": 256 << 20, "AnonPages": 900 << 20},
            net=host_metrics.NetCounters(rx_bytes=5_000_000, tx_bytes=3_000_000),
            disk_io=host_metrics.DiskCounters(
                read_bytes=9_000_000, write_bytes=7_000_000
            ),
        ),
    )
    assert client.get("/metrics").status_code == 401

    body = client.get("/metrics", headers=AUTH).json()
    assert body["mem_total_bytes"] == 4 << 30
    assert body["disk_free_bytes"] == 25 << 30
    assert body["gpu_busy_percent"] == 42.0
    assert body["fan_rpm"] == {"CPU Fan": 2100}
    assert body["apu_power_w"] == 14.0
    assert body["gpu_mem"]["gtt_used_bytes"] == 12 << 30
    assert body["gpu_mem"]["vram_total_bytes"] == 4 << 30
    assert body["mem_breakdown"]["Shmem"] == 256 << 20
    assert body["net"]["rx_bytes"] == 5_000_000
    assert body["disk_io"]["write_bytes"] == 7_000_000
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
