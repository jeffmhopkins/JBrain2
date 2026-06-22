// The standard host-metrics series (CPU / memory / disk / GPU / fan) mapped from
// raw MetricPoints into TimeSeriesPlot's PlotSeries. Shared by the Ops history
// card and the agent's `server_metrics` tool view so both render identically; the
// palette lives here (the component, not model data, owns colors — DESIGN.md).

import type { MetricPoint } from "../api/client";
import type { PlotSeries } from "./TimeSeriesPlot";

const pct = (v: number) => `${Math.round(v)}%`;

function memPct(p: MetricPoint): number | null {
  return p.mem_used_bytes != null && p.mem_total_bytes
    ? (p.mem_used_bytes / p.mem_total_bytes) * 100
    : null;
}

function diskPct(p: MetricPoint): number | null {
  return p.disk_used_bytes != null && p.disk_total_bytes
    ? (p.disk_used_bytes / p.disk_total_bytes) * 100
    : null;
}

export function serverMetricSeries(points: MetricPoint[]): PlotSeries[] {
  return [
    {
      label: "CPU load",
      color: "var(--steel)",
      values: points.map((p) => p.load_1m),
      fmt: (v) => v.toFixed(2),
    },
    { label: "Memory", color: "var(--violet)", values: points.map(memPct), fmt: pct },
    { label: "Disk", color: "var(--amber)", values: points.map(diskPct), fmt: pct },
    {
      label: "GPU",
      color: "var(--location)",
      values: points.map((p) => p.gpu_busy_percent),
      fmt: pct,
    },
    {
      // APU/SoC package power (not wall power).
      label: "APU power",
      color: "var(--green)",
      values: points.map((p) => p.power_w),
      fmt: (v) => `${v.toFixed(1)} W`,
    },
    {
      label: "Fan",
      color: "var(--rose)",
      values: points.map((p) => p.fan_rpm_max),
      fmt: (v) => `${Math.round(v)} rpm`,
    },
  ];
}
