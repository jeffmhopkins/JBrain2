// Heat-oval geometry, kept free of Leaflet so it unit-tests as plain math.
//
// Leaflet.heat only draws circular blobs. To let speed shape the heat map we smear each
// MOVING fix into a dense line of points along its heading, so the blob elongates into
// an oval pointing the way you were going — longer the faster you went. A near-stationary
// fix contributes a single point, so parked spots stay round (and dwell density still
// reads as the hotspot).
//
// The oval is sized against the heat spot's on-screen radius (meters, derived from zoom)
// so it's always visibly elongated when moving regardless of zoom — sizing it in raw
// meters made a fast fix look round when the radius dwarfed the stretch.

export interface LatLon {
  lat: number;
  lon: number;
}

/** Just the fields the oval needs off a LocationFix (so callers can pass fixes directly). */
export interface HeatFix {
  velocity_mps: number | null;
  course_deg: number | null;
}

// Below ~3 mph there's no meaningful heading — keep it a circle.
const HEAT_OVAL_MIN_MPS = 1.4;
// Stretch ≈ how far you'd travel in this many seconds (per side of the oval).
const HEAT_OVAL_SECONDS = 6;
// Absolute cap so highway speed elongates legibly without streaking across the map.
const HEAT_OVAL_MAX_M = 220;
const EARTH_M_PER_DEG = 111_320;

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

/** The heat layer's `[lat, lon, weight]` points: each fix as a blob, plus — when moving —
 * a line of points fore/aft along its heading so the blob becomes a speed-scaled oval.
 * `radiusM` is the spot radius in meters at the current zoom; the oval is sized against
 * it (always ≥ ~1.5 radii each side when moving) so it reads as an oval at any zoom. */
export function heatOvalPoints(
  fixes: HeatFix[],
  pts: LatLon[],
  weight: number,
  radiusM: number,
): [number, number, number][] {
  const out: [number, number, number][] = [];
  // Always clearly elongated when moving, even at low speed / zoomed in.
  const minReach = Math.max(radiusM * 1.5, 12);
  // Step well under the radius so the smeared points overlap into a continuous capsule.
  const step = Math.max(radiusM * 0.5, 10);
  for (let i = 0; i < pts.length; i++) {
    const here = pts[i];
    if (!here) continue;
    out.push([here.lat, here.lon, weight]);
    const v = fixes[i]?.velocity_mps ?? null;
    if (v == null || v < HEAT_OVAL_MIN_MPS) continue;
    const heading = fixHeadingRad(fixes, pts, i);
    if (heading == null) continue;
    const reach = Math.min(Math.max(v * HEAT_OVAL_SECONDS, minReach), HEAT_OVAL_MAX_M);
    const dLatPerM = 1 / EARTH_M_PER_DEG;
    const dLonPerM = 1 / (EARTH_M_PER_DEG * Math.cos((here.lat * Math.PI) / 180));
    const cos = Math.cos(heading);
    const sin = Math.sin(heading);
    // Full-weight points the whole length, both directions: a uniform capsule, not a
    // bright round core with faint tails (which just reads as a circle).
    for (let d = step; d <= reach; d += step) {
      for (const sign of [-1, 1]) {
        const off = sign * d;
        out.push([here.lat + off * cos * dLatPerM, here.lon + off * sin * dLonPerM, weight]);
      }
    }
  }
  return out;
}
