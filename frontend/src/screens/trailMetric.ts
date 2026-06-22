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
// Linear ramps (heading is cyclic and handled as a hue wheel instead).
const RAMP: Record<"speed" | "accel" | "battery" | "timeplace", Stop[]> = {
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
  timeplace: [
    [0, [70, 120, 200]],
    [0.5, [200, 120, 170]],
    [1, [235, 150, 70]],
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
  dwellMax: number,
  buckets: number,
): number {
  let t: number;
  if (metric === "heading") t = (fix.course_deg ?? 0) / 360;
  else if (metric === "timeplace") t = timeplaceT(dwell, timeplaceAnchors(dwellMax));
  else t = (metricValue(metric, fix) ?? 0) / MAXV[metric];
  return Math.round(Math.max(0, Math.min(1, t)) * buckets);
}

export function bucketColor(metric: TrailMetric, bucket: number, buckets: number): string {
  const f = buckets > 0 ? bucket / buckets : 0;
  if (metric === "heading") return `hsl(${Math.round(f * 360)}, 62%, 58%)`;
  return rampColor(metric === "timeplace" ? RAMP.timeplace : RAMP[metric], f);
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

// "Time at place" uses a piecewise scale, not a flat 0→max: fixed minute
// breakpoints (5m / 15m / 1h) each take an equal slice of the colour ramp, then the
// last slice scales to the window's max. So 0–5m occupies a quarter of the ramp and
// short dwells are easy to tell apart, while a multi-hour dwell still reads as "hot".
const TP_BREAKPOINTS_MIN = [0, 5, 15, 60];

/** The dwell breakpoints (minutes) for the current window: the fixed ones below the
 * window max, then the max itself as the top anchor. Evenly spaced on the ramp. */
export function timeplaceAnchors(maxMin: number): number[] {
  const fixed = TP_BREAKPOINTS_MIN.filter((m) => m < maxMin);
  const top = Math.max(maxMin, (fixed[fixed.length - 1] ?? 0) + 0.001);
  return [...fixed, top];
}

/** Map dwell minutes to t∈[0,1] across the (evenly-spaced) anchors. */
function timeplaceT(minutes: number, anchors: number[]): number {
  const n = anchors.length;
  if (n < 2) return 0;
  if (minutes <= (anchors[0] ?? 0)) return 0;
  if (minutes >= (anchors[n - 1] ?? 1)) return 1;
  for (let i = 0; i < n - 1; i++) {
    const a = anchors[i] ?? 0;
    const b = anchors[i + 1] ?? 0;
    if (minutes >= a && minutes <= b) {
      const frac = b > a ? (minutes - a) / (b - a) : 0;
      return (i + frac) / (n - 1);
    }
  }
  return 1;
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
      gradient: cssGradient(RAMP.timeplace),
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
