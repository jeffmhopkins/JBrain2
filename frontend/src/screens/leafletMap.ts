// Leaflet glue for the location Map, isolated from React so the screen stays
// testable (tests mock this module). Tiles come only from the server-side proxy
// (/api/tiles), so the phone never talks to a tile host. Markers are
// circle/polyline/circleMarker — no default icon images to 404 — and their colours
// live in CSS (the `loc-lf-*` classes) to keep tokens-only styling.

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { LocationFix, PlaceGeofence } from "../api/client";

export type MapMode = "live" | "trail" | "heat";

export interface MapState {
  mode: MapMode;
  fixes: LocationFix[];
  places: PlaceGeofence[];
}

export interface LocationMapHandle {
  update: (state: MapState) => void;
  destroy: () => void;
}

const TILE_URL = "/api/tiles/{z}/{x}/{y}.png";

export function createLocationMap(container: HTMLElement): LocationMapHandle {
  const map = L.map(container, { attributionControl: true }).setView([20, 0], 2);
  L.tileLayer(TILE_URL, {
    maxZoom: 19,
    attribution: "© OpenStreetMap contributors",
  }).addTo(map);
  let overlay = L.layerGroup().addTo(map);

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
    } else if (state.mode === "heat") {
      // Overlapping translucent dots read as density — no extra heatmap dep.
      for (const ll of track) {
        L.circleMarker(ll, { radius: 9, stroke: false, className: "loc-lf-heat" }).addTo(overlay);
      }
    } else if (state.mode === "live" && last) {
      L.circleMarker(last, { radius: 7, className: "loc-lf-live" })
        .bindTooltip("latest")
        .addTo(overlay);
    }

    if (bounds.length > 0) {
      map.fitBounds(L.latLngBounds(bounds).pad(0.2), { maxZoom: 16 });
    }
  }

  return {
    update,
    destroy: () => map.remove(),
  };
}
