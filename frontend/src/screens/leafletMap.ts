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
import { withinAccuracy } from "./locationFilter";
import { type TrailMetric, bucketColor, computeDwell, metricBucket } from "./trailMetric";

// Quantizing the colour metric into a handful of buckets lets contiguous same-colour
// stretches draw as ONE polyline, so a long window stays a few runs instead of one
// segment per fix — which keeps the redraws the slider triggers cheap.
const TRAIL_BUCKETS = 12;

export type MapMode = "live" | "trail" | "heat";

// The basemap scheme + URL helpers live in a side-effect-free module so the inline
// tool-views can share them (including the cache-bust token); re-exported here so
// existing `./leafletMap` importers keep working.
export { type TileScheme, readTileScheme, writeTileScheme } from "./tileScheme";
import { type TileScheme, readTileScheme, tileUrl } from "./tileScheme";

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
  // Per-point heat weight (0..1): how much one fix contributes to the density ramp.
  // Defaults to 0.4 when absent.
  heatWeight?: number;
  // Current-location pins (member map). Absent on the owner map — additive.
  pins?: MapPin[];
  // Which metric tints the trail (speed default).
  metric?: TrailMetric;
  // A tapped trail point to highlight + label (null = none).
  selected?: { lat: number; lon: number; label: string; color: string } | null;
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
  // Smoothly pan to a point at the CURRENT zoom — the focused person's map "follows"
  // them as live fixes move them, without the zoom jump of a recenter.
  follow: (lat: number, lon: number) => void;
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

/** Draw the trail as metric-tinted polylines: each segment's bucket is the average of
 * its endpoints' metric buckets, and contiguous same-bucket segments merge into one
 * polyline so a long window stays a few coloured runs. The colour is set inline
 * (per-segment data, not a static token) over the `loc-lf-trail-seg` class, which
 * carries only the width/caps. `dwell` (per-fix minutes) is used only by "timeplace". */
function drawMetricTrail(
  overlay: L.LayerGroup,
  kept: LocationFix[],
  track: L.LatLng[],
  metric: TrailMetric,
  dwell: number[],
  dwellMax: number,
): void {
  let run: L.LatLng[] = [];
  let runBucket = -1;
  const flush = () => {
    if (run.length > 1 && runBucket >= 0) {
      L.polyline(run, {
        className: "loc-lf-trail-seg",
        color: bucketColor(metric, runBucket, TRAIL_BUCKETS),
      }).addTo(overlay);
    }
  };
  for (let i = 0; i < track.length - 1; i++) {
    const a = track[i];
    const b = track[i + 1];
    const fa = kept[i];
    const fb = kept[i + 1];
    if (!a || !b || !fa || !fb) continue;
    const ba = metricBucket(metric, fa, dwell[i] ?? 0, dwellMax, TRAIL_BUCKETS);
    const bb = metricBucket(metric, fb, dwell[i + 1] ?? 0, dwellMax, TRAIL_BUCKETS);
    const bucket = Math.round((ba + bb) / 2);
    if (bucket === runBucket) {
      run.push(b);
    } else {
      flush();
      run = [a, b]; // start the new run at the shared vertex so there is no gap
      runBucket = bucket;
    }
  }
  flush();
}

/** `onSelect` fires when a person pin is tapped (the switcher follows a map tap);
 * `onPointSelect` fires with the nearest fix when the trail is tapped, or with null
 * when empty map is tapped (so a tap off the trail deselects the inspected point). */
export function createLocationMap(
  container: HTMLElement,
  onSelect?: (subjectId: string) => void,
  onPointSelect?: (fix: LocationFix | null) => void,
  scheme: TileScheme = readTileScheme(),
): LocationMapHandle {
  // Zoom moves to the bottom-right so the floating control bar owns the top edge.
  const map = L.map(container, {
    attributionControl: true,
    zoomControl: false,
  }).setView([20, 0], 2);
  L.control.zoom({ position: "bottomright" }).addTo(map);
  // A tap on the basemap (not on the trail/pins — Leaflet doesn't bubble those to the
  // map) clears the inspected point.
  if (onPointSelect) map.on("click", () => onPointSelect(null));
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
    // Drop low-accuracy fixes so jittery indoor GPS doesn't smear the trail into a
    // star-burst (matches the backend geofence accuracy gate). Keep the fixes (not just
    // the coordinates) so the trail can tint each segment by its speed.
    const kept = withinAccuracy(state.fixes);
    const track = kept.map((f) => L.latLng(f.latitude, f.longitude));
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
      const metric = state.metric ?? "speed";
      // "Time at place" needs each fix's dwell minutes; the other metrics ignore it.
      const { dwell, max } = metric === "timeplace" ? computeDwell(kept) : { dwell: [], max: 0 };
      if (track.length > 1) drawMetricTrail(overlay, kept, track, metric, dwell, max);
      L.circleMarker(first, { radius: 5, className: "loc-lf-start" }).addTo(overlay);
      L.circleMarker(last, { radius: 5, className: "loc-lf-end" }).addTo(overlay);
      // An invisible fat hit-line so a tap anywhere on the path selects the nearest
      // fix (the colour ramp's narrow segments are hard to hit directly).
      if (onPointSelect && track.length > 1) {
        const hit = L.polyline(track, {
          className: "loc-lf-hit",
          color: "#000",
          opacity: 0,
          weight: 22,
          // A path bubbles its click to the map by default; that would also fire the
          // basemap's "deselect" handler and cancel the pick. Keep it on the line.
          bubblingMouseEvents: false,
        });
        hit.on("click", (e) => {
          const ll = (e as L.LeafletMouseEvent).latlng;
          let best: LocationFix | undefined;
          let bd = Number.POSITIVE_INFINITY;
          for (let i = 0; i < kept.length; i++) {
            const t = track[i];
            const f = kept[i];
            if (!t || !f) continue;
            const d = map.distance(ll, t);
            if (d < bd) {
              bd = d;
              best = f;
            }
          }
          if (best) onPointSelect(best);
        });
        hit.addTo(overlay);
      }
    } else if (state.mode === "heat" && track.length > 0) {
      // A real gradient heat layer: dwell density reads as the blue→red ramp; the
      // per-point radius and weight are owner-tunable from the Heat control. A modest
      // default weight so transit reads cool and only repeated dwell builds to hot.
      const weight = state.heatWeight ?? 0.4;
      const pts = track.map((ll) => [ll.lat, ll.lng, weight] as [number, number, number]);
      L.heatLayer(pts, {
        radius: state.heatRadius,
        blur: Math.round(state.heatRadius * 0.6),
        maxZoom: 17,
        minOpacity: 0.3,
      }).addTo(overlay);
    } else if (state.mode === "live" && last) {
      // `last` is the newest fix that passed the accuracy gate — a wide-radius
      // latest fix is intentionally not shown as "latest" (trustworthy fixes only).
      L.circleMarker(last, { radius: 7, className: "loc-lf-live" })
        .bindTooltip("latest")
        .addTo(overlay);
    }

    // The tapped/scrubbed trail point: a permanent callout ("J: 35 mph"), tinted by
    // the active metric. Not added to `bounds` — selecting must not re-frame.
    if (state.selected) {
      const sel = state.selected;
      // When the selected point IS the person's current position, its teardrop pin
      // already marks the spot — so skip the redundant ringed dot and lift the callout
      // clear of the pin head (38px tall) instead of sitting on top of its label.
      const onPin = (state.pins ?? []).some(
        (p) => Math.abs(p.lat - sel.lat) < 1e-6 && Math.abs(p.lon - sel.lon) < 1e-6,
      );
      L.circleMarker(L.latLng(sel.lat, sel.lon), {
        radius: onPin ? 0 : 7,
        color: sel.color,
        weight: onPin ? 0 : 3,
        fillColor: "#0e0f11",
        fillOpacity: onPin ? 0 : 1,
        opacity: onPin ? 0 : 1,
        className: "loc-lf-pick",
      })
        .bindTooltip(sel.label, {
          permanent: true,
          direction: "top",
          offset: onPin ? [0, -42] : [0, -8],
          className: "loc-lf-pick-tip",
        })
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
    follow: (lat, lon) => {
      // Pan at the current zoom; skip the work when they're already ~centred so GPS
      // jitter doesn't nudge the view every fix.
      const c = map.getCenter();
      if (Math.abs(c.lat - lat) < 1e-5 && Math.abs(c.lng - lon) < 1e-5) return;
      map.panTo([lat, lon], { animate: true, duration: 0.6 });
      lastFit = null;
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
