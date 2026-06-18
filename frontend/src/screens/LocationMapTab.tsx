// The Map tab of the location surface: a real Leaflet basemap (pan/zoom) over the
// server-side tile proxy — tiles reach the phone only from this box (no tile host
// sees the device). A date-range picker + three modes — Live (latest fix), Trail
// (the path between two dates), Heat (dwell density). Fences from the derived
// mirror draw as context. Reads /api/locations/{devices,places,fixes}; the geofence
// editor files a place note (never the mirror). Leaflet is isolated in leafletMap.ts.

import { useEffect, useRef, useState } from "react";
import { type DeviceSummary, type LocationFix, type PlaceGeofence, api } from "../api/client";
import { Sheet } from "../components/Sheet";
import type { LocationDeps } from "./LocationScreen";
import { type LocationMapHandle, createLocationMap } from "./leafletMap";

type Mode = "live" | "trail" | "heat";

const MODE_LABEL: Record<Mode, string> = { live: "Live", trail: "Trail", heat: "Heat" };

export interface PlaceNoteInput {
  name: string;
  lat: number;
  lon: number;
  radiusM: number;
}

/** The place/correction note the geofence editor files (#7, L10): the owner edits
 * geofences by writing a note, never the mirror table — the analysis pipeline
 * extracts the `geofence` predicate and the projector mirrors it. Worded so the
 * coordinates + radius extract cleanly into the geofence schema. */
export function placeNoteBody(p: PlaceNoteInput): string {
  return (
    `Place geofence: ${p.name} is a circular geofence with a radius of ` +
    `${p.radiusM} meters centered at latitude ${p.lat}, longitude ${p.lon}.`
  );
}

async function defaultFilePlaceNote(p: PlaceNoteInput): Promise<void> {
  await api.createNote({
    client_id: `place-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    domain: "location",
    body: placeNoteBody(p),
  });
}

const DEFAULT_DAYS = 7;

type Meta =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "ready"; devices: DeviceSummary[]; places: PlaceGeofence[] };

type Fixes = { phase: "loading" } | { phase: "error" } | { phase: "done"; fixes: LocationFix[] };

function isoDay(offsetDays: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
}

// The picker's day strings become an inclusive [since, until) window: until is the
// morning after the chosen end day, so the whole end day is covered.
function windowIso(since: string, until: string): { since: string; until: string } {
  const end = new Date(`${until}T00:00:00`);
  end.setDate(end.getDate() + 1);
  return { since: `${since}T00:00:00`, until: end.toISOString() };
}

export function LocationMapTab({ deps }: { deps: LocationDeps | undefined }) {
  const listDevices = deps?.listDevices ?? api.listLocationDevices;
  const listPlaces = deps?.listPlaces ?? api.listLocationPlaces;
  const listFixes = deps?.listFixes ?? api.listLocationFixes;
  const filePlaceNote = deps?.filePlaceNote ?? defaultFilePlaceNote;
  const reverseGeocode = deps?.reverseGeocode ?? api.reverseGeocode;

  const [meta, setMeta] = useState<Meta>({ phase: "loading" });
  const [selected, setSelected] = useState<string>("");
  const [mode, setMode] = useState<Mode>("trail");
  // The Heat view's per-point radius in px (the "spot size" slider). 25 reads as
  // dwell clusters at neighbourhood zoom without smearing the whole track.
  const [heatRadius, setHeatRadius] = useState(25);
  const [since, setSince] = useState(() => isoDay(-DEFAULT_DAYS));
  const [until, setUntil] = useState(() => isoDay(0));
  const [fixes, setFixes] = useState<Fixes>({ phase: "loading" });
  // The Places list lives in a bottom sheet so the map can fill the screen.
  const [placesOpen, setPlacesOpen] = useState(false);
  // The geofence editor target: a blank form ("new") or an existing place to
  // file a correction note against.
  const [editing, setEditing] = useState<PlaceGeofence | "new" | null>(null);
  // The latest fix's on-box street address (Wave 4c). Best-effort: stays null when
  // the geocoder is off, so the caption simply doesn't render.
  const [address, setAddress] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    Promise.all([listDevices(), listPlaces()])
      .then(([devices, places]) => {
        if (stale) return;
        setMeta({ phase: "ready", devices, places });
        setSelected((s) => s || devices[0]?.id || "");
      })
      .catch(() => {
        if (!stale) setMeta({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [listDevices, listPlaces]);

  useEffect(() => {
    if (!selected) return;
    let stale = false;
    setFixes({ phase: "loading" });
    const w = windowIso(since, until);
    listFixes(selected, w.since, w.until)
      .then((rows) => {
        if (!stale) setFixes({ phase: "done", fixes: rows });
      })
      .catch(() => {
        if (!stale) setFixes({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [selected, since, until, listFixes]);

  // Name the latest fix in plain words via the on-box geocoder (best-effort).
  useEffect(() => {
    const rows = fixes.phase === "done" ? fixes.fixes : [];
    const latest = rows[rows.length - 1];
    if (!latest) {
      setAddress(null);
      return;
    }
    let stale = false;
    reverseGeocode(latest.latitude, latest.longitude)
      .then((a) => {
        if (!stale) setAddress(a);
      })
      .catch(() => {
        if (!stale) setAddress(null);
      });
    return () => {
      stale = true;
    };
  }, [fixes, reverseGeocode]);

  if (meta.phase === "loading") return <p className="analysis-quiet">loading map…</p>;
  if (meta.phase === "error") {
    return <p className="analysis-quiet">couldn't load the map — check the connection.</p>;
  }
  if (meta.devices.length === 0) {
    return <p className="analysis-quiet">no devices yet — add one on the Devices tab.</p>;
  }

  const points = fixes.phase === "done" ? fixes.fixes : [];

  return (
    <div className="loc-map">
      <div className="loc-map-stage">
        <LeafletMap mode={mode} fixes={points} places={meta.places} heatRadius={heatRadius} />

        <div className="loc-map-overlay loc-map-overlay-top">
          {meta.devices.length > 1 && (
            <select
              className="loc-map-device"
              aria-label="Device"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              {meta.devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.label}
                </option>
              ))}
            </select>
          )}
          <div className="seg-row" role="tablist" aria-label="Map mode">
            {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
              <button
                key={m}
                type="button"
                role="tab"
                aria-selected={mode === m}
                className={`seg${mode === m ? " seg-on" : ""}`}
                onClick={() => setMode(m)}
              >
                {MODE_LABEL[m]}
              </button>
            ))}
          </div>
          <div className="loc-map-range">
            <input
              type="date"
              aria-label="From date"
              value={since}
              max={until}
              onChange={(e) => setSince(e.target.value)}
            />
            <span className="loc-map-range-sep">→</span>
            <input
              type="date"
              aria-label="To date"
              value={until}
              min={since}
              onChange={(e) => setUntil(e.target.value)}
            />
          </div>
          {mode === "heat" && (
            <div className="loc-map-heat-ctl">
              <label htmlFor="loc-heat-radius">Spot size</label>
              <input
                id="loc-heat-radius"
                type="range"
                min={10}
                max={50}
                step={1}
                value={heatRadius}
                aria-label="Heat spot size"
                onChange={(e) => setHeatRadius(Number(e.target.value))}
              />
            </div>
          )}
        </div>

        <div className="loc-map-overlay loc-map-overlay-bottom" aria-live="polite">
          {address && points.length > 0 && (
            <p className="loc-map-address">
              <span aria-hidden="true">📍</span> latest near {address}
            </p>
          )}
          {fixes.phase === "loading" && <p className="loc-map-note">loading fixes…</p>}
          {fixes.phase === "error" && (
            <p className="loc-map-note">couldn't load fixes for this range.</p>
          )}
          {fixes.phase === "done" && points.length === 0 && (
            <p className="loc-map-note">no fixes in this range.</p>
          )}
        </div>

        <button type="button" className="loc-map-places-btn" onClick={() => setPlacesOpen(true)}>
          Places{meta.places.length > 0 ? ` · ${meta.places.length}` : ""}
        </button>
      </div>

      {placesOpen && (
        <Sheet title="Places" onClose={() => setPlacesOpen(false)}>
          <p className="loc-sheet-note">
            Geofenced places draw on the map and drive arrival/departure crossings.
          </p>
          <button
            type="button"
            className="loc-places-add"
            onClick={() => {
              setPlacesOpen(false);
              setEditing("new");
            }}
          >
            ＋ Add place
          </button>
          {meta.places.length === 0 ? (
            <p className="analysis-quiet">no places yet — add one to start a geofence.</p>
          ) : (
            <div className="loc-places-list">
              {meta.places.map((p) => (
                <button
                  key={p.place_entity_id}
                  type="button"
                  className="loc-place-row"
                  onClick={() => {
                    setPlacesOpen(false);
                    setEditing(p);
                  }}
                  // A place with no geometry at all is malformed — nothing to edit.
                  disabled={!p.center && !p.polygon}
                >
                  <span className="loc-place-name">{p.name}</span>
                  <span className="loc-place-meta">
                    {p.center && p.radius_m !== null
                      ? `${Math.round(p.radius_m)} m radius`
                      : "area"}
                  </span>
                </button>
              ))}
            </div>
          )}
        </Sheet>
      )}

      {editing && (
        <PlaceEditor
          place={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onFile={filePlaceNote}
        />
      )}
    </div>
  );
}

function PlaceEditor({
  place,
  onClose,
  onFile,
}: {
  place: PlaceGeofence | null;
  onClose: () => void;
  onFile: (p: PlaceNoteInput) => Promise<void>;
}) {
  const [name, setName] = useState(place?.name ?? "");
  const [lat, setLat] = useState(place?.center ? String(place.center.lat) : "");
  const [lon, setLon] = useState(place?.center ? String(place.center.lon) : "");
  const [radius, setRadius] = useState(place?.radius_m != null ? String(place.radius_m) : "150");
  const [busy, setBusy] = useState(false);
  const [filed, setFiled] = useState(false);
  const [failed, setFailed] = useState(false);

  const valid =
    name.trim() !== "" &&
    lat.trim() !== "" &&
    lon.trim() !== "" &&
    Number.isFinite(Number(lat)) &&
    Number.isFinite(Number(lon)) &&
    Number(radius) > 0;

  async function submit(): Promise<void> {
    if (!valid || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      await onFile({
        name: name.trim(),
        lat: Number(lat),
        lon: Number(lon),
        radiusM: Number(radius),
      });
      setFiled(true);
    } catch {
      setFailed(true);
      setBusy(false);
    }
  }

  if (filed) {
    return (
      <Sheet title="Place filed" onClose={onClose}>
        <p className="loc-sheet-note">
          Filed as a place note. JBrain reads your notes, so {name.trim()} appears on the map once
          it's processed — usually within a minute.
        </p>
        <div className="loc-sheet-actions">
          <button type="button" className="primary" onClick={onClose}>
            Done
          </button>
        </div>
      </Sheet>
    );
  }

  return (
    <Sheet title={place ? `Edit ${place.name}` : "Add place"} onClose={onClose}>
      <p className="loc-sheet-note">
        Geofences are kept as notes (your sources of truth) — saving files a place note; the map
        updates from it. Never edits the map directly.
      </p>
      <input
        // biome-ignore lint/a11y/noAutofocus: a deliberately-summoned sheet form
        autoFocus
        aria-label="Place name"
        placeholder="place name (e.g. Home)…"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <div className="loc-editor-row">
        <input
          aria-label="Latitude"
          placeholder="latitude"
          inputMode="decimal"
          value={lat}
          onChange={(e) => setLat(e.target.value)}
        />
        <input
          aria-label="Longitude"
          placeholder="longitude"
          inputMode="decimal"
          value={lon}
          onChange={(e) => setLon(e.target.value)}
        />
      </div>
      <input
        aria-label="Radius (meters)"
        placeholder="radius (meters)"
        inputMode="numeric"
        value={radius}
        onChange={(e) => setRadius(e.target.value)}
      />
      {failed && <p className="loc-sheet-error">couldn't file the note — try again.</p>}
      <div className="loc-sheet-actions">
        <button type="button" className="ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          disabled={!valid || busy}
          onClick={() => void submit()}
        >
          {busy ? "Filing…" : "File place note"}
        </button>
      </div>
    </Sheet>
  );
}

function LeafletMap({
  mode,
  fixes,
  places,
  heatRadius,
}: {
  mode: Mode;
  fixes: LocationFix[];
  places: PlaceGeofence[];
  heatRadius: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const handle = useRef<LocationMapHandle | null>(null);

  // Create the map once the container is in the DOM; tear it down on unmount.
  // (This component only mounts in the ready branch, so the container exists.)
  useEffect(() => {
    if (!ref.current) return;
    handle.current = createLocationMap(ref.current);
    return () => {
      handle.current?.destroy();
      handle.current = null;
    };
  }, []);

  // Redraw overlays whenever the data, mode, or heat radius changes.
  useEffect(() => {
    handle.current?.update({ mode, fixes, places, heatRadius });
  }, [mode, fixes, places, heatRadius]);

  return <div ref={ref} className="loc-map-canvas" aria-label="Location map" />;
}
