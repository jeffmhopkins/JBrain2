// How far away a heard station is — measured from the phone, on the phone.
//
// The owner's call: range and bearing come from the PWA's own location rather than the
// box's. That is the better anchor for a hand-held screen, and it has a property the
// box's position does not: **the coordinate never leaves the device.** The distance is
// computed here, from a position the browser hands us and we never send anywhere. The
// server is told nothing, stores nothing, and logs nothing about where the reader is.
//
// It is also allowed to fail, and failing is the common case, not the edge: permission
// denied, a desktop with no GPS, a cold fix that has not landed yet. So every caller
// gets `null` and falls back to the grid square, which needs no location at all.
//
// ONE MORE HONESTY: this measures from where the READER is, not from where the box is.
// Away from home, a station the box heard next door reads as a hundred miles off. That
// is true rather than wrong — it answers "how far is that from me" — but it is why the
// detail panel spells out "from you" rather than leaving the anchor implied.

export interface Fix {
  lat: number;
  lon: number;
  /** Metres of uncertainty the browser reported. A 3 km fix must not print a tenth of
   *  a mile as though it meant something. */
  accuracy: number;
}

const EARTH_MILES = 3958.8;
const POINTS = [
  "N",
  "NNE",
  "NE",
  "ENE",
  "E",
  "ESE",
  "SE",
  "SSE",
  "S",
  "SSW",
  "SW",
  "WSW",
  "W",
  "WNW",
  "NW",
  "NNW",
];

/** Great-circle distance in miles, and the compass point to look along. */
export function rangeAndBearing(
  from: Fix,
  lat: number,
  lon: number,
): { miles: number; point: string } {
  const rad = (d: number) => (d * Math.PI) / 180;
  const [p1, p2] = [rad(from.lat), rad(lat)];
  const dPhi = rad(lat - from.lat);
  const dLam = rad(lon - from.lon);
  const a = Math.sin(dPhi / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dLam / 2) ** 2;
  const miles = 2 * EARTH_MILES * Math.asin(Math.min(1, Math.sqrt(a)));
  const y = Math.sin(dLam) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dLam);
  const bearing = (Math.atan2(y, x) * 180) / Math.PI;
  const point = POINTS[Math.round(((bearing + 360) % 360) / 22.5) % 16] ?? "N";
  return { miles, point };
}

/** The range line as it reads on a row, or null when it would be a false precision.
 *
 * Rounding tracks the distance: tenths close in, whole miles further out, because
 * "12.3 mi" from a consumer GPS claims a metre of certainty nobody has. Under the fix's
 * own accuracy it says "here" instead of a number — a station 40 m away measured with a
 * 3 km fix is not 0.02 miles away, it is somewhere in this town. */
export function rangeLine(from: Fix, lat: number, lon: number): string {
  const { miles, point } = rangeAndBearing(from, lat, lon);
  if (miles * 1609 < Math.max(from.accuracy, 30)) return "here";
  if (miles < 10) return `${miles.toFixed(1)} mi ${point}`;
  return `${Math.round(miles)} mi ${point}`;
}

/** Ask the browser once. Resolves to null on any failure, including a refusal.
 *
 * `maximumAge` accepts a two-minute-old fix: this is a list of packets heard over hours,
 * so a fresh satellite lock buys nothing and costs a visible delay and battery. */
export function askWhereYouAre(timeoutMs = 8000): Promise<Fix | null> {
  if (typeof navigator === "undefined" || !navigator.geolocation) {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    let settled = false;
    const done = (fix: Fix | null) => {
      if (!settled) {
        settled = true;
        resolve(fix);
      }
    };
    // Belt and braces: some browsers never call either callback when a permission
    // prompt is dismissed rather than answered, which would leave the row waiting for
    // ever on a promise that cannot reject.
    const timer = setTimeout(() => done(null), timeoutMs);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        clearTimeout(timer);
        done({
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
        });
      },
      () => {
        clearTimeout(timer);
        done(null);
      },
      { enableHighAccuracy: false, timeout: timeoutMs, maximumAge: 120_000 },
    );
  });
}
