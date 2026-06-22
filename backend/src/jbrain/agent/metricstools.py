"""The agent's read-only server-metrics tool (`query_server_metrics`).

Lets the assistant ground answers about the machine's own health — "has the box
been running hot, throttling, or low on memory lately?" — in the recorded host
telemetry rather than guessing. Read-only and owner-only by construction: it
reads the owner-only `host_metrics` tables through the session's RLS scope
(`ToolContext.session`), so a non-owner session simply sees no samples. This is
host hardware telemetry, never the owner's notes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from jbrain import ops_metrics
from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# The same windows the Ops graph offers (jbrain.api.ops._HISTORY_RANGES).
_RANGES: dict[str, timedelta] = {
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "2d": timedelta(days=2),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "1y": timedelta(days=365),
}
_DEFAULT_RANGE = "24h"
_GIB = float(1 << 30)


def _stats(values: list[float]) -> tuple[float, float, float] | None:
    """`(latest, peak, avg)` over the non-null values, or None when empty."""
    if not values:
        return None
    return values[-1], max(values), sum(values) / len(values)


def _series(points: list[dict[str, Any]], key: str) -> list[float]:
    return [float(p[key]) for p in points if p.get(key) is not None]


def _pct_series(points: list[dict[str, Any]], used_key: str, total_key: str) -> list[float]:
    return [
        100.0 * float(p[used_key]) / float(p[total_key])
        for p in points
        if p.get(used_key) is not None and p.get(total_key)
    ]


def _format(range_label: str, data: dict[str, Any]) -> str:
    points: list[dict[str, Any]] = data["points"]
    if not points:
        return f"No host-metrics samples were recorded in the last {range_label}."

    lines = [
        f"Server health over the last {range_label} ({len(points)} {data['resolution']} buckets):"
    ]

    load = _stats(_series(points, "load_1m"))
    if load:
        lines.append(f"- CPU load (1m): now {load[0]:.2f}, peak {load[1]:.2f}, avg {load[2]:.2f}")

    mem = _stats(_pct_series(points, "mem_used_bytes", "mem_total_bytes"))
    if mem:
        total = next((p["mem_total_bytes"] for p in points if p.get("mem_total_bytes")), None)
        cap = f" of {total / _GIB:.0f} GiB" if total else ""
        lines.append(
            f"- Memory used: now {mem[0]:.0f}%, peak {mem[1]:.0f}%, avg {mem[2]:.0f}%{cap}"
        )

    disk = _stats(_pct_series(points, "disk_used_bytes", "disk_total_bytes"))
    if disk:
        lines.append(f"- Disk used: now {disk[0]:.0f}%, peak {disk[1]:.0f}%")

    gpu = _stats(_series(points, "gpu_busy_percent"))
    if gpu:
        lines.append(f"- GPU busy: now {gpu[0]:.0f}%, peak {gpu[1]:.0f}%, avg {gpu[2]:.0f}%")

    power = _stats(_series(points, "power_w"))
    if power:
        # APU/SoC package power, not wall power.
        lines.append(
            f"- APU power: now {power[0]:.1f} W, peak {power[1]:.1f} W, avg {power[2]:.1f} W"
        )

    fan = _stats(_series(points, "fan_rpm_max"))
    if fan:
        lines.append(f"- Fan (hottest): now {fan[0]:.0f} rpm, peak {fan[1]:.0f} rpm")

    swap = _stats(_series(points, "swap_used_bytes"))
    if swap and swap[1] > 0:
        lines.append(f"- Swap used: now {swap[0] / _GIB:.1f} GiB, peak {swap[1] / _GIB:.1f} GiB")

    return "\n".join(lines)


def _metrics_view(range_label: str, data: dict[str, Any]) -> ViewPayload:
    """The data-only twin of the prose summary: a `server_metrics` view the app
    renders as the same sparkline stack the Ops screen draws (DESIGN.md "Agent
    tool views"). Carries the raw points; the component owns colors/formatting."""
    return ViewPayload(
        view="server_metrics",
        surface="inline",
        data={
            "range": range_label,
            "resolution": data["resolution"],
            "points": data["points"],
        },
    )


def build_metrics_handlers(
    maker: async_sessionmaker[AsyncSession],
) -> dict[str, ToolHandler]:
    """The `query_server_metrics` tool, bound to the app's sessionmaker. The
    handler runs the read under `ctx.session` (the turn's RLS scope), so the
    owner-only firewall on the metrics tables — not this code — is the gate."""

    async def query_server_metrics_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        requested = str(arguments.get("range", _DEFAULT_RANGE)).strip() or _DEFAULT_RANGE
        window = _RANGES.get(requested)
        if window is None:
            return ToolOutput(
                f"'{requested}' isn't a known range. Use one of: {', '.join(_RANGES)}."
            )
        data = await ops_metrics.history(maker, ctx.session, since=datetime.now(UTC) - window)
        # The prose grounds the model's reasoning; the view (when there's data) is
        # the human-facing graph rendered in the chat bubble.
        view = _metrics_view(requested, data) if data["points"] else None
        return ToolOutput(_format(requested, data), view=view)

    return {"query_server_metrics": query_server_metrics_tool}
