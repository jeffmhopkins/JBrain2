// Speed display + the speed→colour ramp for trails, shared by the live map
// (MemberDashboard), the owner console (LocationScreen / map), and the Leaflet glue
// so the "moving" cutoff and the palette stay identical everywhere. Speeds are stored
// in m/s; the UI shows mph.

// Below this a fix reads as "not traveling" — GPS jitter shows a crawl at rest, so the
// status hides speed rather than report a meaningless 1 mph.
export const TRAVELING_MIN_MPS = 5 / 3.6; // 5 km/h
export const MPS_TO_MPH = 2.2369363;
// Top of the speed ramp: at/above this a segment is fully "hot".
export const SPEED_MAX_MPH = 60;

/** A speed (m/s) as an "N mph" status string, or null when not traveling (no speed
 * reported, or below the stationary cutoff). */
export function travelingSpeedMph(velocityMps: number | null): string | null {
  if (velocityMps === null || velocityMps < TRAVELING_MIN_MPS) return null;
  return `${Math.round(velocityMps * MPS_TO_MPH)} mph`;
}

// The "turbo" blue→red ramp (the chosen option): blue (slow) → cyan → green → amber →
// red (fast). Stops mirror the app mock so the legend and the trail agree.
const RAMP: [number, [number, number, number]][] = [
  [0, [58, 110, 200]],
  [0.3, [60, 180, 170]],
  [0.55, [120, 200, 90]],
  [0.78, [230, 190, 70]],
  [1, [224, 86, 79]],
];

/** A CSS `rgb()` colour for a speed (m/s) along the turbo ramp; null speed reads as
 * the slow end. */
export function speedColor(velocityMps: number | null): string {
  const t = Math.max(0, Math.min(1, ((velocityMps ?? 0) * MPS_TO_MPH) / SPEED_MAX_MPH));
  for (let i = 0; i < RAMP.length - 1; i++) {
    const lo = RAMP[i];
    const hi = RAMP[i + 1];
    if (!lo || !hi) continue;
    const [s0, c0] = lo;
    const [s1, c1] = hi;
    if (t >= s0 && t <= s1) {
      const k = (t - s0) / (s1 - s0 || 1);
      const ch = (a: number, b: number) => Math.round(a + (b - a) * k);
      return `rgb(${ch(c0[0], c1[0])}, ${ch(c0[1], c1[1])}, ${ch(c0[2], c1[2])})`;
    }
  }
  const end = RAMP[RAMP.length - 1];
  const c = end ? end[1] : [224, 86, 79];
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}
