import { describe, expect, it } from "vitest";
import type { MetricPoint } from "../api/client";
import { serverMetricSeries } from "./serverMetricSeries";

function point(over: Partial<MetricPoint> = {}): MetricPoint {
  return {
    t: "2026-07-17T00:00:00Z",
    load_1m: 0.5,
    load_1m_max: 0.9,
    load_5m: 0.5,
    load_15m: 0.5,
    mem_used_bytes: 64 * 2 ** 30,
    mem_used_max_bytes: 80 * 2 ** 30,
    mem_total_bytes: 128 * 2 ** 30,
    swap_used_bytes: 0,
    disk_used_bytes: 500 * 2 ** 30,
    disk_used_max_bytes: 500 * 2 ** 30,
    disk_total_bytes: 2000 * 2 ** 30,
    gpu_busy_percent: 40,
    gpu_busy_max: 70,
    fan_rpm_max: 2100,
    power_w: 14,
    power_w_max: 30,
    net_rx_bps: 5 * 2 ** 20,
    net_tx_bps: 1 * 2 ** 20,
    disk_read_bps: 30 * 2 ** 20,
    disk_write_bps: 12 * 2 ** 20,
    ...over,
  };
}

describe("serverMetricSeries", () => {
  it("combines network and disk I/O into one two-line panel each", () => {
    const series = serverMetricSeries([point()]);
    const net = series.find((s) => s.label === "Network");
    const disk = series.find((s) => s.label === "Disk I/O");
    expect(net?.lines.map((l) => l.label)).toEqual(["down", "up"]);
    expect(disk?.lines.map((l) => l.label)).toEqual(["read", "write"]);
    // The two directions are separate colored lines in the same panel.
    expect(net?.lines[0]?.color).not.toBe(net?.lines[1]?.color);
  });

  it("attaches the bucket-max as a band on single-series charts", () => {
    const series = serverMetricSeries([point()]);
    const mem = series.find((s) => s.label === "Memory");
    // line = avg (64/128 = 50%), band = max (80/128 = 62.5%).
    expect(mem?.lines[0]?.values[0]).toBeCloseTo(50);
    expect(mem?.lines[0]?.band?.[0]).toBeCloseTo(62.5);
    const gpu = series.find((s) => s.label === "GPU");
    expect(gpu?.lines[0]?.values[0]).toBe(40);
    expect(gpu?.lines[0]?.band?.[0]).toBe(70);
  });

  it("formats throughput as bytes/sec at 1024-base", () => {
    const net = serverMetricSeries([point()]).find((s) => s.label === "Network");
    expect(net?.fmt(5 * 2 ** 20)).toBe("5.0 MB/s");
    expect(net?.fmt(150 * 2 ** 20)).toBe("150 MB/s");
  });

  it("passes through nulls (no counter yet) rather than zeroing", () => {
    const series = serverMetricSeries([point({ disk_write_bps: null })]);
    const disk = series.find((s) => s.label === "Disk I/O");
    const write = disk?.lines.find((l) => l.label === "write");
    expect(write?.values[0]).toBeNull();
  });
});
