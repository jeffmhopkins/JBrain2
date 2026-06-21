// Leaflet glue for the location Map, isolated from React so the screen stays
// testable (tests mock this module). Tiles come only from the server-side proxy
// (/api/tiles), so the phone never talks to a tile host. Markers are
// circle/polyline/circleMarker — no default icon images to 404 — and their colours
// live in CSS (the `loc-lf-*` classes) to keep tokens-only styling.

import L from "leaflet";
import "leaflet/dist/leaflet.css";
// Side-effect import: augments L with `heatLayer` (the gradient Heat view).
import "leaflet.heat";
import type { LocationFix, PlaceGeofence } from "../api/client";

export type MapMode = "live" | "trail" | "heat";

// The selectable basemap styles. Each maps to a separate server-side scheme
// (/api/tiles/{scheme}/…) with its own upstream + cache, so the toggle only swaps
// the tiles — never the app's own dark UI chrome.
export type TileScheme = "dark" | "light";

const TILE_SCHEME_KEY = "jbrain.map.tileScheme";
const DEFAULT_TILE_SCHEME: TileScheme = "dark";

/** The owner's last basemap choice, persisted so a reload (or a tab/app switch)
 * keeps it. Defaults to dark — matching the app UI — and tolerates a missing or
 * blocked localStorage (private mode / WebView). */
export function readTileScheme(): TileScheme {
  try {
    return localStorage.getItem(TILE_SCHEME_KEY) === "light" ? "light" : DEFAULT_TILE_SCHEME;
  } catch {
    return DEFAULT_TILE_SCHEME;
  }
}

export function writeTileScheme(scheme: TileScheme): void {
  try {
    localStorage.setItem(TILE_SCHEME_KEY, scheme);
  } catch {
    // A blocked store just means the choice isn't remembered — never a crash.
  }
}

function tileUrl(scheme: TileScheme): string {
  return `/api/tiles/${scheme}/{z}/{x}/{y}.png`;
}

/** A person's current-location pin (the member map's switcher targets). Colour is a
 * palette class (`loc-pin-c*`) so it stays tokens-only; the initial is drawn in the
 * teardrop. */
export interface MapPin {
  subjectId: string;
  lat: number;
  lon: number;
  label: string;
  colorClass: string;
  live: boolean;
  selected: boolean;
}

export interface MapState {
  mode: MapMode;
  fixes: LocationFix[];
  places: PlaceGeofence[];
  // Per-point heat radius in px (the "spot size" the Heat control tunes).
  heatRadius: number;
  // Current-location pins (member map). Absent on the owner map — additive.
  pins?: MapPin[];
  // Auto-fit the view to the data. Default true (the owner map). The member map
  // sets it false when focused on one person, so a redraw (live fix, mode/window
  // change) doesn't fight the centerOn / the user's pan.
  autoFit?: boolean;
}

export interface LocationMapHandle {
  update: (state: MapState) => void;
  // Recenter on a point (the member map's tap-a-person-to-center), keeping at least
  // a street-level zoom.
  centerOn: (lat: number, lon: number) => void;
  // Swap the basemap style in place (the light/dark tile toggle). Re-points the
  // existing tile layer, so overlays/markers stay put.
  setScheme: (scheme: TileScheme) => void;
  destroy: () => void;
}

function escapeHtml(s: string): string {
  return s.replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c,
  );
}

/** `onSelect` fires when a person pin is tapped, so the switcher can follow a tap on
 * the map. */
export function createLocationMap(
  container: HTMLElement,
  onSelect?: (subjectId: string) => void,
  scheme: TileScheme = readTileScheme(),
): LocationMapHandle {
  // Zoom moves to the bottom-right so the floating control bar owns the top edge.
  const map = L.map(container, {
    attributionControl: true,
    zoomControl: false,
  }).setView([20, 0], 2);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  const tiles = L.tileLayer(tileUrl(scheme), {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors © CARTO",
  }).addTo(map);
  let currentScheme = scheme;
  let overlay = L.layerGroup().addTo(map);
  // The data bounds last auto-fitted, so a redraw that doesn't change them leaves
  // the owner's manual zoom/pan untouched.
  let lastFit: L.LatLngBounds | null = null;

  // The map fills a flex container; Leaflet only re-measures on window resize, so
  // a tab switch or rotation that resizes the container would otherwise leave it
  // mis-sized (grey gutters). Re-measure whenever the container's box changes.
  const resize = new ResizeObserver(() => map.invalidateSize());
  resize.observe(container);

  function update(state: MapState): void {
    overlay.remove();
    overlay = L.layerGroup().addTo(map);
    const track = state.fixes.map((f) => L.latLng(f.latitude, f.longitude));
    const bounds: L.LatLng[] = [...track];

    for (const place of state.places) {
      if (place.center && place.radius_m !== null) {
        const c = L.latLng(place.center.lat, place.center.lon);
        L.circle(c, { radius: place.radius_m, className: "loc-lf-fence" })
          .bindTooltip(place.name)
          .addTo(overlay);
        bounds.push(c);
      } else if (place.polygon && place.polygon.length > 0) {
        const ring = place.polygon.map((p) => L.latLng(p.lat, p.lon));
        L.polygon(ring, { className: "loc-lf-fence" }).bindTooltip(place.name).addTo(overlay);
        bounds.push(...ring);
      }
    }

    // Current-location pins (member map): a coloured teardrop per visible person.
    for (const pin of state.pins ?? []) {
      const c = L.latLng(pin.lat, pin.lon);
      const cls = [
        "loc-pin",
        pin.colorClass,
        pin.live ? "is-live" : "is-stale",
        pin.selected ? "is-sel" : "",
      ].join(" ");
      const icon = L.divIcon({
        className: "loc-pin-wrap",
        html: `<div class="${cls}"><span class="loc-pin-head"><b>${escapeHtml(
          (pin.label[0] ?? "?").toUpperCase(),
        )}</b></span></div>`,
        iconSize: [30, 38],
        iconAnchor: [15, 38],
      });
      const marker = L.marker(c, { icon, title: pin.label }).addTo(overlay);
      if (onSelect) marker.on("click", () => onSelect(pin.subjectId));
      bounds.push(c);
    }

    const first = track[0];
    const last = track[track.length - 1];
    if (state.mode === "trail" && first && last) {
      if (track.length > 1) L.polyline(track, { className: "loc-lf-trail" }).addTo(overlay);
      L.circleMarker(first, { radius: 5, className: "loc-lf-start" }).addTo(overlay);
      L.circleMarker(last, { radius: 5, className: "loc-lf-end" }).addTo(overlay);
    } else if (state.mode === "heat" && track.length > 0) {
      // A real gradient heat layer: dwell density reads as the blue→red ramp,
      // and the per-point radius is owner-tunable from the Heat control.
      // A modest per-point intensity so transit reads cool and only repeated
      // dwell at a spot builds up to the hot end of the ramp.
      const pts = track.map((ll) => [ll.lat, ll.lng, 0.4] as [number, number, number]);
      L.heatLayer(pts, {
        radius: state.heatRadius,
        blur: Math.round(state.heatRadius * 0.6),
        maxZoom: 17,
        minOpacity: 0.3,
      }).addTo(overlay);
    } else if (state.mode === "live" && last) {
      L.circleMarker(last, { radius: 7, className: "loc-lf-live" })
        .bindTooltip("latest")
        .addTo(overlay);
    }

    if (bounds.length > 0 && (state.autoFit ?? true)) {
      // Auto-fit only when the framed area actually changes (new device, range,
      // or fixes). A redraw from a control tweak — the heat spot-size slider, a
      // mode switch — must keep the owner's current zoom/pan.
      const next = L.latLngBounds(bounds).pad(0.2);
      if (!lastFit || !lastFit.equals(next)) {
        map.fitBounds(next, { maxZoom: 16 });
        lastFit = next;
      }
    }
  }

  return {
    update,
    centerOn: (lat, lon) => {
      map.setView([lat, lon], Math.max(map.getZoom(), 15), { animate: true });
      lastFit = null; // a manual recenter; a later autoFit (Everyone) should re-fit
    },
    setScheme: (next) => {
      if (next === currentScheme) return;
      currentScheme = next;
      tiles.setUrl(tileUrl(next)); // re-requests the grid under the new scheme's path
    },
    destroy: () => {
      resize.disconnect();
      map.remove();
    },
  };
}
