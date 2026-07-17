"""The query_server_metrics agent tool: range validation and the summary it
hands the model. The DB read (ops_metrics.history) is faked — its real behavior
is integration-tested in test_host_metrics."""

from typing import Any

import pytest

from jbrain.agent import metricstools
from jbrain.agent.loop import ToolOutput
from jbrain.db.session import SessionContext


class _Ctx:
    """A minimal ToolContext stand-in carrying just the session scope the tool reads."""

    session = SessionContext(principal_id="p", principal_kind="owner")


def _point(**over: Any) -> dict[str, Any]:
    base = {
        "t": "2026-06-22T00:00:00+00:00",
        "load_1m": 0.5,
        "mem_used_bytes": 64 << 30,
        "mem_total_bytes": 128 << 30,
        "disk_used_bytes": 500 << 30,
        "disk_total_bytes": 2000 << 30,
        "gpu_busy_percent": 40.0,
        "fan_rpm_max": 2000,
        "power_w": 14.0,
        "swap_used_bytes": 0,
        "net_rx_bps": 8 << 20,
        "net_tx_bps": 2 << 20,
        "disk_read_bps": 30 << 20,
        "disk_write_bps": 12 << 20,
    }
    base.update(over)
    return base


async def test_summarizes_each_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    points = [
        _point(load_1m=0.5, fan_rpm_max=2000, power_w=12.0, net_rx_bps=8 << 20),
        _point(load_1m=2.0, fan_rpm_max=3000, power_w=28.0, net_rx_bps=100 << 20),
    ]

    async def fake_history(maker, ctx, *, since, until=None, max_points=300):  # noqa: ANN001, ANN202
        return {"resolution": "raw", "points": points}

    monkeypatch.setattr(metricstools.ops_metrics, "history", fake_history)
    handler = metricstools.build_metrics_handlers(object())["query_server_metrics"]  # type: ignore[arg-type]

    out = await handler({"range": "24h"}, _Ctx())  # type: ignore[arg-type]

    assert "last 24h" in out
    assert "CPU load (1m): now 2.00, peak 2.00" in out  # latest is the 2.0 point
    assert "Memory used: now 50%" in out
    assert "Fan (hottest): now 3000 rpm, peak 3000 rpm" in out
    assert "APU power: now 28.0 W, peak 28.0 W" in out
    # Network + disk throughput report their peak (100 MiB/s down here).
    assert "Network: down peak 100.0 MiB/s, up peak 2.0 MiB/s" in out
    assert "Disk I/O: read peak 30.0 MiB/s, write peak 12.0 MiB/s" in out
    # It also emits a server_metrics view carrying the raw points for the chart.
    assert isinstance(out, ToolOutput)
    assert out.view is not None
    assert out.view.view == "server_metrics"
    assert out.view.surface == "inline"
    assert out.view.data["range"] == "24h"
    assert out.view.data["points"] == points


async def test_empty_history_is_stated(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_history(maker, ctx, *, since, until=None, max_points=300):  # noqa: ANN001, ANN202
        return {"resolution": "raw", "points": []}

    monkeypatch.setattr(metricstools.ops_metrics, "history", fake_history)
    handler = metricstools.build_metrics_handlers(object())["query_server_metrics"]  # type: ignore[arg-type]

    out = await handler({"range": "7d"}, _Ctx())  # type: ignore[arg-type]
    assert "No host-metrics samples" in out
    assert isinstance(out, ToolOutput)
    assert out.view is None  # nothing to plot


async def test_unknown_range_is_rejected_without_querying(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def fake_history(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal called
        called = True
        return {"resolution": "raw", "points": []}

    monkeypatch.setattr(metricstools.ops_metrics, "history", fake_history)
    handler = metricstools.build_metrics_handlers(object())["query_server_metrics"]  # type: ignore[arg-type]

    out = await handler({"range": "nonsense"}, _Ctx())  # type: ignore[arg-type]
    assert "isn't a known range" in out
    assert called is False  # validation happens before any DB read
