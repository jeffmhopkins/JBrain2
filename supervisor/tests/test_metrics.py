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


def test_read_host_metrics_parses_proc(tmp_path: Path) -> None:
    (tmp_path / "meminfo").write_text(MEMINFO)
    (tmp_path / "loadavg").write_text("0.42 0.30 0.18 1/123 4567\n")
    (tmp_path / "uptime").write_text("86400.55 170000.00\n")

    m = host_metrics.read_host_metrics(proc=tmp_path, disk_path="/")

    assert m.mem_total_bytes == 4030000 * 1024
    assert m.mem_available_bytes == 1500000 * 1024
    assert m.swap_total_bytes == 2097152 * 1024
    assert m.load_1m == 0.42
    assert m.load_15m == 0.18
    assert m.uptime_seconds == 86400
    assert m.disk_total_bytes > 0


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
        ),
    )
    assert client.get("/metrics").status_code == 401

    body = client.get("/metrics", headers=AUTH).json()
    assert body["mem_total_bytes"] == 4 << 30
    assert body["disk_free_bytes"] == 25 << 30
    assert body["containers"]
    assert body["containers"][0]["mem_bytes"] == 100 << 20
