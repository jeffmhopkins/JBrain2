// Leaflet glue for the inline location tool-views (location_map / place_card),
// isolated from React so the views stay testable — the view vitest mocks this
// module (jsdom has no layout engine), exactly like screens/LocationScreen.test
// mocks screens/leafletMap. Tiles come only from the on-box /api/tiles proxy, so
// the phone never talks to a tile host. Each leg is its OWN polyline — a GPS gap
// is never bridged into one line. Marker colours live in CSS (the teal loc-tv-*
// classes), keeping tokens-only styling.

import L from "leaflet";
import "leaflet/dist/leaflet.css";

const TILE_URL = "/api/tiles/{z}/{x}/{y}.png";

/** A trail leg: a polyline of [lat, lon] pairs (render-only coordinates). */
export interface TrailLegData {
  points: [number, number][];
}

export interface InlineMapHandle {
  invalidate: () => void;
  destroy: () => void;
}

function baseMap(container: HTMLElement, interactive: boolean): L.Map {
  const map = L.map(container, {
    attributionControl: interactive,
    zoomControl: interactive,
    dragging: interactive,
    scrollWheelZoom: false,
    doubleClickZoom: interactive,
    boxZoom: interactive,
    keyboard: interactive,
    // `tap` is removed in newer Leaflet; cast to keep the option-bag typed loosely.
  } as L.MapOptions);
  L.tileLayer(TILE_URL, {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  return map;
}

/** Draw a gap-split trail: one polyline per leg (never across a gap), a green
 * start dot on the first leg's first point, a teal end dot on the last leg's
 * last point, and fit to all points. `interactive` toggles pan/zoom (the
 * thumbnail is static; the expanded map is live). */
export function renderTrail(
  container: HTMLElement,
  legs: TrailLegData[],
  { interactive }: { interactive: boolean },
): InlineMapHandle {
  const map = baseMap(container, interactive);
  const all: L.LatLng[] = [];
  for (const leg of legs) {
    const line = leg.points.map(([lat, lon]) => L.latLng(lat, lon));
    if (line.length > 1) {
      L.polyline(line, { className: "loc-tv-trail" }).addTo(map);
    }
    all.push(...line);
  }
  const first = legs[0]?.points[0];
  const lastLeg = legs[legs.length - 1];
  const last = lastLeg?.points[lastLeg.points.length - 1];
  if (first) {
    L.circleMarker(L.latLng(first[0], first[1]), { radius: 5, className: "loc-tv-start" }).addTo(
      map,
    );
  }
  if (last) {
    L.circleMarker(L.latLng(last[0], last[1]), { radius: 6, className: "loc-tv-end" }).addTo(map);
  }
  if (all.length === 1 && all[0]) {
    map.setView(all[0], 15);
  } else if (all.length > 1) {
    map.fitBounds(L.latLngBounds(all).pad(0.2), { maxZoom: 16 });
  }
  return { invalidate: () => map.invalidateSize(), destroy: () => map.remove() };
}

/** A single-place mini-map: a teal fence circle + a green centre dot, for
 * place_card. The centre is render-only. */
export function renderPlace(
  container: HTMLElement,
  center: [number, number],
  radiusM: number | null,
): InlineMapHandle {
  const map = baseMap(container, false);
  const c = L.latLng(center[0], center[1]);
  if (radiusM !== null) {
    L.circle(c, { radius: radiusM, className: "loc-tv-fence" }).addTo(map);
  }
  L.circleMarker(c, { radius: 5, className: "loc-tv-start" }).addTo(map);
  map.setView(c, 15);
  return { invalidate: () => map.invalidateSize(), destroy: () => map.remove() };
}
