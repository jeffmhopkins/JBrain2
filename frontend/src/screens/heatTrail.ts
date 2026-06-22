// Heat-trail geometry, kept free of Leaflet so it unit-tests as plain math.
//
// Leaflet.heat only draws circular blobs, so a moving drive would otherwise read as a
// string of disconnected dots. To let movement shape the heat we fill the gap BETWEEN
// consecutive fixes — when they're far enough apart to be travel (not parked jitter) and
// close enough to be one continuous leg (not an offline gap) — with interpolated points
// along the real path. The elongation falls out of the route itself: a fast leg is a long
// ribbon, a stop (clustered fixes) stays a round dwell blob. No headings, so there are no
// satellite "spokes" fanning out of a parked cluster.

export interface LatLon {
  lat: number;
  lon: number;
}

const EARTH_M_PER_DEG = 111_320;
// Shorter than this between fixes is parked GPS jitter — leave it as a dwell blob.
const MIN_TRAVEL_M = 10;
// Longer than this is a gap (parked/offline between samples) — don't bridge it with a line.
const MAX_TRAVEL_M = 400;

function metersBetween(a: LatLon, b: LatLon): number {
  const north = (b.lat - a.lat) * EARTH_M_PER_DEG;
  const east =
    (b.lon - a.lon) * EARTH_M_PER_DEG * Math.cos((((a.lat + b.lat) / 2) * Math.PI) / 180);
  return Math.hypot(north, east);
}

/** The heat layer's `[lat, lon, weight]` points: every fix, plus points interpolated along
 * each travelled segment so the path reads as a continuous ribbon. `spacingM` is how far
 * apart to drop the in-between points (≈ a fraction of the spot radius at the current zoom)
 * so the ribbon stays smooth without exploding the point count. */
export function heatTrailPoints(
  pts: LatLon[],
  weight: number,
  spacingM: number,
): [number, number, number][] {
  const out: [number, number, number][] = [];
  const step = Math.max(spacingM, 6);
  for (let i = 0; i < pts.length; i++) {
    const here = pts[i];
    if (!here) continue;
    out.push([here.lat, here.lon, weight]);
    const next = pts[i + 1];
    if (!next) continue;
    const d = metersBetween(here, next);
    if (d < MIN_TRAVEL_M || d > MAX_TRAVEL_M) continue; // parked jitter, or a gap to skip
    const segments = Math.floor(d / step);
    for (let k = 1; k < segments; k++) {
      const t = k / segments;
      out.push([
        here.lat + (next.lat - here.lat) * t,
        here.lon + (next.lon - here.lon) * t,
        weight,
      ]);
    }
  }
  return out;
}
