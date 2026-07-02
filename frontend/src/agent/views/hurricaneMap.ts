// Leaflet glue for the hurricane_card Track tab, isolated from React so the view
// stays testable — the view's vitest mocks this module (jsdom has no layout engine),
// exactly like locationMap.ts. Tiles come only from the on-box /api/tiles proxy, so
// the phone never talks to a tile host. The storm's track + cone are public NHC
// coordinates and the `you` pin is the geocoded city centre (the scoped #9 relaxation
// documented in backend hurricanetools.py). Overlay colours live in CSS (the
// `tv-hu-lf-*` classes) to keep tokens-only styling; markers are circleMarker/polyline/
// polygon so there are no default icon images to 404.

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { tileUrl } from "../../screens/tileScheme";

/** One forecast point in geographic space, plus the vitals the map tones by. */
export interface HuTrackPointGeo {
  lat: number;
  lon: number;
  label: string;
  cat: string;
  past: boolean;
}

/** A bare geographic point (cone vertex / the `you` pin). */
export interface HuGeoPoint {
  lat: number;
  lon: number;
}

export interface HuMapData {
  track: HuTrackPointGeo[];
  cone: HuGeoPoint[];
  you: HuGeoPoint | null;
}

export interface HuMapHandle {
  invalidate: () => void;
  destroy: () => void;
}

// Inline tool-view maps always use the dark basemap (matching the app UI); the
// light/dark toggle lives on the interactive location maps, not these cards.
const TILE_URL = tileUrl("dark");

/** Draw the storm on real map tiles: the cone polygon, the forecast path, its points
 * (toned by Saffir-Simpson category, past positions muted), and the `you` pin — then
 * frame the whole thing. The card is embedded in a scrolling chat, so the wheel does
 * NOT zoom (it would hijack the page scroll); drag pans and the +/- buttons or a pinch
 * zoom, like the expanded location map. The default Leaflet attribution control is off —
 * on a small phone card its banner overlapped the storm; the required OSM/CARTO credit
 * is carried by a compact static caption under the map instead (see HuTrackMap). */
export function renderHurricaneMap(container: HTMLElement, data: HuMapData): HuMapHandle {
  const map = L.map(container, {
    attributionControl: false,
    zoomControl: true,
    scrollWheelZoom: false,
  });
  L.tileLayer(TILE_URL, { maxZoom: 12 }).addTo(map);

  const bounds: L.LatLng[] = [];

  // The cone (a filled forecast-uncertainty polygon) needs ≥3 vertices to be an area.
  if (data.cone.length >= 3) {
    const ring = data.cone.map((p) => L.latLng(p.lat, p.lon));
    L.polygon(ring, { className: "tv-hu-lf-cone" }).addTo(map);
    bounds.push(...ring);
  }

  // The forecast path connects the points in track order (already tau-sorted on-box).
  const path = data.track.map((p) => L.latLng(p.lat, p.lon));
  if (path.length >= 2) {
    L.polyline(path, { className: "tv-hu-lf-path" }).addTo(map);
  }

  for (const p of data.track) {
    const c = L.latLng(p.lat, p.lon);
    const cls = `tv-hu-lf-pt cat-${p.cat || "0"}${p.past ? " past" : ""}`;
    L.circleMarker(c, { radius: p.past ? 4 : 6, className: cls })
      .bindTooltip(p.label, { direction: "top", offset: [0, -4] })
      .addTo(map);
    bounds.push(c);
  }

  if (data.you) {
    const c = L.latLng(data.you.lat, data.you.lon);
    L.circleMarker(c, { radius: 6, className: "tv-hu-lf-you" })
      .bindTooltip("you", { direction: "top", offset: [0, -4] })
      .addTo(map);
    bounds.push(c);
  }

  // Frame the storm: a padded fit for a real extent, a fixed regional zoom for the lone
  // single-point case, and a sane world view when there is nothing to draw.
  if (bounds.length === 1 && bounds[0]) {
    map.setView(bounds[0], 5);
  } else if (bounds.length > 1) {
    map.fitBounds(L.latLngBounds(bounds).pad(0.25), { maxZoom: 9 });
  } else {
    map.setView([20, -60], 3);
  }

  return { invalidate: () => map.invalidateSize(), destroy: () => map.remove() };
}
