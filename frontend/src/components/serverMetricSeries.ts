// The standard host-metrics series (CPU / memory / disk / GPU / fan) mapped from
// raw MetricPoints into TimeSeriesPlot's PlotSeries. Shared by the Ops history
// card and the agent's `server_metrics` tool view so both render identically; the
// palette lives here (the component, not model data, owns colors — DESIGN.md).

import type { MetricPoint } from "../api/client";
import type { PlotSeries } from "./TimeSeriesPlot";

const pct = (v: number) => `${Math.round(v)}%`;

// Throughput bytes/sec -> human, 1024-base to match the memory/disk readouts.
function rate(v: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let n = v;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${i > 0 && n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}/s`;
}

const ratio = (used: number | null, total: number | null): number | null =>
  used != null && total ? (used / total) * 100 : null;

export function serverMetricSeries(points: MetricPoint[]): PlotSeries[] {
  return [
    {
      label: "CPU load",
      lines: [
        {
          color: "var(--steel)",
          values: points.map((p) => p.load_1m),
          band: points.map((p) => p.load_1m_max),
        },
      ],
      fmt: (v) => v.toFixed(2),
    },
    {
      label: "Memory",
      lines: [
        {
          color: "var(--violet)",
          values: points.map((p) => ratio(p.mem_used_bytes, p.mem_total_bytes)),
          band: points.map((p) => ratio(p.mem_used_max_bytes, p.mem_total_bytes)),
        },
      ],
      fmt: pct,
    },
    {
      label: "Disk",
      lines: [
        {
          color: "var(--amber)",
          values: points.map((p) => ratio(p.disk_used_bytes, p.disk_total_bytes)),
          band: points.map((p) => ratio(p.disk_used_max_bytes, p.disk_total_bytes)),
        },
      ],
      fmt: pct,
    },
    {
      label: "GPU",
      lines: [
        {
          color: "var(--location)",
          values: points.map((p) => p.gpu_busy_percent),
          band: points.map((p) => p.gpu_busy_max),
        },
      ],
      fmt: pct,
    },
    {
      // APU/SoC package power (not wall power).
      label: "APU power",
      lines: [
        {
          color: "var(--green)",
          values: points.map((p) => p.power_w),
          band: points.map((p) => p.power_w_max),
        },
      ],
      fmt: (v) => `${v.toFixed(1)} W`,
    },
    {
      label: "Fan",
      lines: [{ color: "var(--rose)", values: points.map((p) => p.fan_rpm_max) }],
      fmt: (v) => `${Math.round(v)} rpm`,
    },
    {
      // Down + up share one panel so the two directions are read together.
      label: "Network",
      lines: [
        { label: "down", color: "var(--periwinkle)", values: points.map((p) => p.net_rx_bps) },
        { label: "up", color: "var(--orchid)", values: points.map((p) => p.net_tx_bps) },
      ],
      fmt: rate,
    },
    {
      label: "Disk I/O",
      lines: [
        { label: "read", color: "var(--sage)", values: points.map((p) => p.disk_read_bps) },
        { label: "write", color: "var(--terracotta)", values: points.map((p) => p.disk_write_bps) },
      ],
      fmt: rate,
    },
  ];
}
