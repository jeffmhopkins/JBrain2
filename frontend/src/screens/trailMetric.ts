// The trail's color metrics — speed / accel / battery / heading / time-at-place —
// shared by the Leaflet trail renderer and the React legend so colors and scales
// stay identical. Speeds are stored m/s; everything is presented per the metric.

import type { LocationFix } from "../api/client";
import { MPS_TO_MPH } from "./speed";

export type TrailMetric = "speed" | "accel" | "battery" | "heading" | "timeplace";
export const TRAIL_METRICS: TrailMetric[] = ["speed", "accel", "battery", "heading", "timeplace"];
export const METRIC_LABEL: Record<TrailMetric, string> = {
  speed: "Speed",
  accel: "Accel",
  battery: "Battery",
  heading: "Heading",
  timeplace: "Time at place",
};

type Stop = [number, [number, number, number]];
// Linear ramps (heading is a hue wheel; time-at-place is banded — both handled apart).
const RAMP: Record<"speed" | "accel" | "battery", Stop[]> = {
  speed: [
    [0, [58, 110, 200]],
    [0.3, [60, 180, 170]],
    [0.55, [120, 200, 90]],
    [0.78, [230, 190, 70]],
    [1, [224, 86, 79]],
  ],
  accel: [
    [0, [91, 191, 138]],
    [0.5, [230, 198, 106]],
    [1, [224, 86, 79]],
  ],
  // battery: full = green (good) on the right, empty = red on the left.
  battery: [
    [0, [224, 86, 79]],
    [0.5, [230, 198, 106]],
    [1, [91, 191, 138]],
  ],
};
const MAXV: Record<"speed" | "accel" | "battery", number> = { speed: 60, accel: 4, battery: 100 };

function rampColor(stops: Stop[], at: number): string {
  const t = Math.max(0, Math.min(1, at));
  for (let i = 0; i < stops.length - 1; i++) {
    const lo = stops[i];
    const hi = stops[i + 1];
    if (!lo || !hi) continue;
    const [s0, c0] = lo;
    const [s1, c1] = hi;
    if (t >= s0 && t <= s1) {
      const k = (t - s0) / (s1 - s0 || 1);
      const ch = (a: number, b: number) => Math.round(a + (b - a) * k);
      return `rgb(${ch(c0[0], c1[0])}, ${ch(c0[1], c1[1])}, ${ch(c0[2], c1[2])})`;
    }
  }
  const end = stops[stops.length - 1];
  const c = end ? end[1] : [120, 120, 120];
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}
const cssGradient = (stops: Stop[]) =>
  `linear-gradient(90deg,${stops.map(([p, c]) => `rgb(${c.join(",")}) ${Math.round(p * 100)}%`).join(",")})`;
function hueGradient(): string {
  const s: string[] = [];
  for (let h = 0; h <= 360; h += 30) s.push(`hsl(${h},62%,58%) ${Math.round((h / 360) * 100)}%`);
  return `linear-gradient(90deg,${s.join(",")})`;
}

/** A fix's value in the metric's native units (mph / m/s² / % / ° / minutes), or
 * null when the device didn't report it. `dwell` is required for "timeplace". */
export function metricValue(metric: TrailMetric, fix: LocationFix, dwell = 0): number | null {
  switch (metric) {
    case "speed":
      return fix.velocity_mps == null ? null : fix.velocity_mps * MPS_TO_MPH;
    case "accel":
      return fix.acceleration_mps2;
    case "battery":
      return fix.battery_pct;
    case "heading":
      return fix.course_deg;
    case "timeplace":
      return dwell;
  }
}

/** A short label for the tap-to-inspect callout, e.g. "35 mph" / "NE 47°" / "8 min". */
const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
export function metricLabel(metric: TrailMetric, fix: LocationFix, dwell = 0): string {
  const v = metricValue(metric, fix, dwell);
  if (v == null) return "—";
  switch (metric) {
    case "speed":
      return `${Math.round(v)} mph`;
    case "accel":
      return `${v.toFixed(1)} m/s²`;
    case "battery":
      return `${Math.round(v)}%`;
    case "heading":
      return `${COMPASS[Math.round(v / 45) % 8]} ${Math.round(v)}°`;
    case "timeplace":
      return `${Math.round(v)} min`;
  }
}

// Quantize a fix to a color bucket (so contiguous same-bucket trail stretches merge
// into one polyline). `buckets` is the number of color steps.
export function metricBucket(
  metric: TrailMetric,
  fix: LocationFix,
  dwell: number,
  _dwellMax: number,
  buckets: number,
): number {
  // Time-at-place is banded, so the "bucket" is the dwell band index (not a ramp step);
  // contiguous same-band stretches then merge into one polyline like the others.
  if (metric === "timeplace") return timeplaceBand(dwell);
  let t: number;
  if (metric === "heading") t = (fix.course_deg ?? 0) / 360;
  else t = (metricValue(metric, fix) ?? 0) / MAXV[metric];
  return Math.round(Math.max(0, Math.min(1, t)) * buckets);
}

export function bucketColor(metric: TrailMetric, bucket: number, buckets: number): string {
  if (metric === "heading") {
    const f = buckets > 0 ? bucket / buckets : 0;
    return `hsl(${Math.round(f * 360)}, 62%, 58%)`;
  }
  if (metric === "timeplace") {
    const c = TP_BAND_COLORS[Math.max(0, Math.min(TP_BAND_COLORS.length - 1, Math.round(bucket)))];
    return c ? `rgb(${c[0]}, ${c[1]}, ${c[2]})` : "rgb(120, 120, 120)";
  }
  return rampColor(RAMP[metric], buckets > 0 ? bucket / buckets : 0);
}

/** The exact colour for a single fix (a finer quantization than the trail's), for the
 * tap-to-inspect callout. */
export function colorForFix(
  metric: TrailMetric,
  fix: LocationFix,
  dwell: number,
  dwellMax: number,
): string {
  const n = 32;
  return bucketColor(metric, metricBucket(metric, fix, dwell, dwellMax, n), n);
}

// "Time at place" is BANDED, not a smooth ramp: each dwell interval gets its own
// distinct hue so the boundaries read as hard colour changes (a 7-min stay is plainly
// a different colour from a 40-min one). The bands are fixed in minutes (independent of
// the window) so a colour always means the same dwell. The breakpoints are 5m/15m/1h.
const TP_BREAKPOINTS_MIN = [0, 5, 15, 60];
const TP_BAND_COLORS: [number, number, number][] = [
  [102, 170, 224], // 0–5m   · blue
  [110, 200, 120], // 5–15m  · green
  [235, 200, 90], //  15m–1h · amber
  [224, 86, 79], //   1h+    · red
];

/** The dwell band (0..3) for a duration in minutes. */
function timeplaceBand(minutes: number): number {
  if (minutes < 5) return 0;
  if (minutes < 15) return 1;
  if (minutes < 60) return 2;
  return 3;
}

/** The breakpoints visible for a window: the fixed ones below the max, then the max
 * itself (the band boundaries, for the legend ticks). */
export function timeplaceAnchors(maxMin: number): number[] {
  const fixed = TP_BREAKPOINTS_MIN.filter((m) => m < maxMin);
  const top = Math.max(maxMin, (fixed[fixed.length - 1] ?? 0) + 0.001);
  return [...fixed, top];
}

/** A hard-stepped gradient of the bands present in the window — equal-width solid
 * blocks, so the legend shows the bands (not a blend) and aligns with the ticks. */
function bandedGradient(maxMin: number): string {
  const n = TP_BREAKPOINTS_MIN.filter((m) => m < maxMin).length || 1;
  const stops: string[] = [];
  for (let i = 0; i < n; i++) {
    const c = TP_BAND_COLORS[i] ?? TP_BAND_COLORS[TP_BAND_COLORS.length - 1] ?? [120, 120, 120];
    const col = `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
    stops.push(`${col} ${(i / n) * 100}%`, `${col} ${((i + 1) / n) * 100}%`);
  }
  return `linear-gradient(90deg,${stops.join(",")})`;
}

function fmtMinutes(m: number): string {
  if (m <= 0) return "0";
  if (m < 1) return `${Math.round(m * 60)}s`;
  if (m < 60) return `${Math.round(m)}m`;
  const h = m / 60;
  return Number.isInteger(h) ? `${h}h` : `${h.toFixed(1)}h`;
}

export interface LegendInfo {
  label: string;
  unit: string;
  gradient: string;
  // Labels evenly spaced under the ramp: 2 = the ends; more = breakpoints.
  ticks: string[];
}
export function legendInfo(metric: TrailMetric, dwellMax: number): LegendInfo {
  if (metric === "heading")
    return {
      label: "Heading",
      unit: "compass",
      gradient: hueGradient(),
      ticks: ["N", "E", "S", "W", "N"],
    };
  if (metric === "timeplace")
    return {
      label: "Time at place",
      unit: "min near",
      gradient: bandedGradient(dwellMax),
      ticks: timeplaceAnchors(dwellMax).map(fmtMinutes),
    };
  const unit = metric === "speed" ? "mph" : metric === "accel" ? "m/s²" : "%";
  const hi = metric === "speed" ? "60+" : metric === "accel" ? "4" : "100";
  return {
    label: METRIC_LABEL[metric],
    unit,
    gradient: cssGradient(RAMP[metric]),
    ticks: ["0", hi],
  };
}

/** "Time at place": minutes spent within ~100 m of each fix. Uses a ~100 m grid so
 * it is O(n) (not O(n²)) — each fix's dwell is the total time of all fixes in its
 * cell. Returns a per-fix array (parallel to `fixes`) and the window max. */
export function computeDwell(fixes: LocationFix[]): { dwell: number[]; max: number } {
  const n = fixes.length;
  if (n === 0) return { dwell: [], max: 0 };
  const CELL = 0.0009; // ° latitude ≈ 100 m
  const cellOf = (f: LocationFix) => {
    const cl = Math.cos((f.latitude * Math.PI) / 180) || 1;
    return `${Math.round(f.latitude / CELL)}:${Math.round((f.longitude * cl) / CELL)}`;
  };
  const cells: string[] = new Array(n);
  const cellMin = new Map<string, number>();
  for (let i = 0; i < n; i++) {
    const f = fixes[i] as LocationFix;
    cells[i] = cellOf(f);
    const t = new Date(f.captured_at).getTime();
    const prev = fixes[i - 1];
    const next = fixes[i + 1];
    // This fix "owns" half the gap to each neighbour.
    let durMs = 0;
    if (prev) durMs += (t - new Date(prev.captured_at).getTime()) / 2;
    if (next) durMs += (new Date(next.captured_at).getTime() - t) / 2;
    const key = cells[i] as string;
    cellMin.set(key, (cellMin.get(key) ?? 0) + durMs / 60000);
  }
  const dwell = cells.map((c) => cellMin.get(c) ?? 0);
  const max = dwell.reduce((m, v) => Math.max(m, v), 0);
  return { dwell, max };
}
