import { describe, expect, it } from "vitest";
import type { MetricPoint } from "../api/client";
import { serverMetricSeries } from "./serverMetricSeries";

function point(over: Partial<MetricPoint> = {}): MetricPoint {
  return {
    t: "2026-07-17T00:00:00Z",
    load_1m: 0.5,
    load_5m: 0.5,
    load_15m: 0.5,
    mem_used_bytes: 64 * 2 ** 30,
    mem_total_bytes: 128 * 2 ** 30,
    swap_used_bytes: 0,
    disk_used_bytes: 500 * 2 ** 30,
    disk_total_bytes: 2000 * 2 ** 30,
    gpu_busy_percent: 40,
    fan_rpm_max: 2100,
    power_w: 14,
    net_rx_bps: 5 * 2 ** 20,
    net_tx_bps: 1 * 2 ** 20,
    disk_read_bps: 30 * 2 ** 20,
    disk_write_bps: 12 * 2 ** 20,
    ...over,
  };
}

describe("serverMetricSeries", () => {
  it("exposes the network and disk-I/O throughput series", () => {
    const series = serverMetricSeries([point()]);
    const labels = series.map((s) => s.label);
    expect(labels).toEqual(
      expect.arrayContaining(["Net down", "Net up", "Disk read", "Disk write"]),
    );
  });

  it("maps the byte counters to their series and formats bytes/sec", () => {
    const [p] = [point({ net_rx_bps: 5 * 2 ** 20 })];
    const netDown = serverMetricSeries([p]).find((s) => s.label === "Net down");
    expect(netDown?.values[0]).toBe(5 * 2 ** 20);
    // 5 MiB/s renders in MB/s at 1024-base, one decimal below 10.
    expect(netDown?.fmt(5 * 2 ** 20)).toBe("5.0 MB/s");
    // A round hundreds figure drops the decimal above 10.
    expect(netDown?.fmt(150 * 2 ** 20)).toBe("150 MB/s");
  });

  it("passes through nulls (no counter yet) rather than zeroing", () => {
    const series = serverMetricSeries([point({ disk_write_bps: null })]);
    const diskWrite = series.find((s) => s.label === "Disk write");
    expect(diskWrite?.values[0]).toBeNull();
  });
});
