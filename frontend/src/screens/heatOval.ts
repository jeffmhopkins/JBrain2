// Heat-oval geometry, kept free of Leaflet so it unit-tests as plain math.
//
// Leaflet.heat only draws circular blobs. To let speed shape the heat map we smear each
// MOVING fix into a few satellite points fore/aft along its heading, so the blob
// elongates into an oval pointing the way you were going — longer the faster you went.
// A near-stationary fix contributes a single point, so parked spots stay round (and the
// dwell density still reads as the hotspot).

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
const HEAT_OVAL_SECONDS = 4;
// Cap so highway speed elongates legibly without streaking across the map.
const HEAT_OVAL_MAX_M = 120;
// Satellites carry less weight than the fix itself, so the oval reads as a smear of the
// hotspot rather than a hotter spot.
const SATELLITE_WEIGHT = 0.5;
const EARTH_M_PER_DEG = 111_320;
const SATELLITE_FRACTIONS = [-1, -0.5, 0.5, 1];

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
 * satellite points along its heading so the blob becomes a speed-scaled oval. */
export function heatOvalPoints(
  fixes: HeatFix[],
  pts: LatLon[],
  weight: number,
): [number, number, number][] {
  const out: [number, number, number][] = [];
  for (let i = 0; i < pts.length; i++) {
    const here = pts[i];
    if (!here) continue;
    out.push([here.lat, here.lon, weight]);
    const v = fixes[i]?.velocity_mps ?? null;
    if (v == null || v < HEAT_OVAL_MIN_MPS) continue;
    const heading = fixHeadingRad(fixes, pts, i);
    if (heading == null) continue;
    const reach = Math.min(v * HEAT_OVAL_SECONDS, HEAT_OVAL_MAX_M);
    const dLatPerM = 1 / EARTH_M_PER_DEG;
    const dLonPerM = 1 / (EARTH_M_PER_DEG * Math.cos((here.lat * Math.PI) / 180));
    for (const frac of SATELLITE_FRACTIONS) {
      const d = frac * reach;
      const lat = here.lat + d * Math.cos(heading) * dLatPerM;
      const lon = here.lon + d * Math.sin(heading) * dLonPerM;
      out.push([lat, lon, weight * SATELLITE_WEIGHT]);
    }
  }
  return out;
}
