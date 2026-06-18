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

export interface MapState {
  mode: MapMode;
  fixes: LocationFix[];
  places: PlaceGeofence[];
  // Per-point heat radius in px (the "spot size" the Heat control tunes).
  heatRadius: number;
}

export interface LocationMapHandle {
  update: (state: MapState) => void;
  destroy: () => void;
}

const TILE_URL = "/api/tiles/{z}/{x}/{y}.png";

export function createLocationMap(container: HTMLElement): LocationMapHandle {
  // Zoom moves to the bottom-right so the floating control bar owns the top edge.
  const map = L.map(container, {
    attributionControl: true,
    zoomControl: false,
  }).setView([20, 0], 2);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  L.tileLayer(TILE_URL, {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
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

    if (bounds.length > 0) {
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
    destroy: () => {
      resize.disconnect();
      map.remove();
    },
  };
}
