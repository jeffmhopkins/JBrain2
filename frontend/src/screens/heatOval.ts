// Heat-oval geometry, kept free of Leaflet so it unit-tests as plain math.
//
// Leaflet.heat only draws circular blobs. To let speed shape the heat map we smear each
// MOVING fix into a dense line of full-weight points along its heading, so the blob
// elongates into an oval pointing the way you were going — longer the faster you went.
// A near-stationary fix contributes a single point, so parked spots stay round (and
// dwell density still reads as the hotspot).
//
// Length is sized in MULTIPLES OF THE SPOT RADIUS (not raw metres) so the oval reads
// the same at any zoom — anchoring it to metres made a fast fix look round when the
// pixel radius dwarfed the stretch. Speed scales it from ~1.5 radii (barely moving) up
// to ~7.5 radii (highway), so the aspect ratio is unmistakable. Speed comes from the
// fix's reported velocity, falling back to the spacing to its neighbours (≈ a fixed
// sample interval) so the oval still forms when a fix lacks a velocity.

export interface LatLon {
  lat: number;
  lon: number;
}

/** Just the fields the oval needs off a LocationFix (so callers can pass fixes directly). */
export interface HeatFix {
  velocity_mps: number | null;
  course_deg: number | null;
}

const EARTH_M_PER_DEG = 111_320;
// Below ~2 mph there's no meaningful heading — keep it a circle.
const HEAT_OVAL_MIN_MPS = 1.0;
// Speed (m/s) at which the oval reaches full length (~63 mph saturates it).
const SPEED_AT_FULL_MPS = 28;
// Half-length of the oval, in spot-radii: this many when barely moving...
const REACH_BASE_RADII = 1.5;
// ...plus up to this many more at full speed (so highway ≈ 7.5 radii each side).
const REACH_SPAN_RADII = 6;
// Neighbour-distance speed fallback assumes the moving sample cadence (see LocationService).
const SAMPLE_INTERVAL_S = 5;

function metersBetween(a: LatLon, b: LatLon): number {
  const north = (b.lat - a.lat) * EARTH_M_PER_DEG;
  const east =
    (b.lon - a.lon) * EARTH_M_PER_DEG * Math.cos((((a.lat + b.lat) / 2) * Math.PI) / 180);
  return Math.hypot(north, east);
}

/** Travel heading in radians (0 = north, clockwise) for point `i`: the fix's reported
 * course when known, else the bearing from the previous point. Null when neither is
 * available (no course and no prior point / a zero-length step). */
export function fixHeadingRad(fixes: HeatFix[], pts: LatLon[], i: number): number | null {
  const course = fixes[i]?.course_deg;
  if (course != null) return (course * Math.PI) / 180;
  const a = pts[i - 1];
  const b = pts[i];
  if (!a || !b) return null;
  const north = b.lat - a.lat;
  const east = (b.lon - a.lon) * Math.cos((b.lat * Math.PI) / 180);
  if (north === 0 && east === 0) return null;
  return Math.atan2(east, north); // bearing from north, clockwise
}

/** Speed (m/s) at point `i`: the fix's reported velocity, else estimated from the larger
 * gap to an adjacent fix over the sample interval (so a missing velocity still elongates). */
export function fixSpeedMps(fixes: HeatFix[], pts: LatLon[], i: number): number | null {
  const reported = fixes[i]?.velocity_mps;
  if (reported != null) return reported;
  const here = pts[i];
  if (!here) return null;
  const prev = pts[i - 1];
  const next = pts[i + 1];
  const gap = Math.max(prev ? metersBetween(prev, here) : 0, next ? metersBetween(here, next) : 0);
  return gap > 0 ? gap / SAMPLE_INTERVAL_S : null;
}

/** The heat layer's `[lat, lon, weight]` points: each fix as a blob, plus — when moving —
 * a dense line of points fore/aft along its heading so the blob becomes a speed-scaled
 * oval. `radiusM` is the spot radius in metres at the current zoom; the oval length is a
 * multiple of it, so it reads as an oval at any zoom. */
export function heatOvalPoints(
  fixes: HeatFix[],
  pts: LatLon[],
  weight: number,
  radiusM: number,
): [number, number, number][] {
  const out: [number, number, number][] = [];
  // Step well under the radius so the smeared points overlap into a continuous capsule.
  const step = Math.max(radiusM * 0.4, 8);
  for (let i = 0; i < pts.length; i++) {
    const here = pts[i];
    if (!here) continue;
    out.push([here.lat, here.lon, weight]);
    const v = fixSpeedMps(fixes, pts, i);
    if (v == null || v < HEAT_OVAL_MIN_MPS) continue;
    const heading = fixHeadingRad(fixes, pts, i);
    if (heading == null) continue;
    const speedFrac = Math.min(v / SPEED_AT_FULL_MPS, 1);
    const reach = radiusM * (REACH_BASE_RADII + REACH_SPAN_RADII * speedFrac);
    const dLatPerM = 1 / EARTH_M_PER_DEG;
    const dLonPerM = 1 / (EARTH_M_PER_DEG * Math.cos((here.lat * Math.PI) / 180));
    const cos = Math.cos(heading);
    const sin = Math.sin(heading);
    for (let d = step; d <= reach; d += step) {
      for (const sign of [-1, 1]) {
        const off = sign * d;
        out.push([here.lat + off * cos * dLatPerM, here.lon + off * sin * dLonPerM, weight]);
      }
    }
  }
  return out;
}
