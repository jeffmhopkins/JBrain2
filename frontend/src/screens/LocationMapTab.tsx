// The Map tab of the location surface: a self-rendered schematic map (no tile
// servers, L1) with a date-range picker and three modes — Live (latest fix),
// Trail (the path between two dates), and Heat (dwell density). Fences from the
// derived mirror draw as context in every mode. Reads /api/locations/{devices,
// places,fixes}; the geofence editor (writing a place note) lands in 5d-iii.

import { useEffect, useState } from "react";
import { type DeviceSummary, type LocationFix, type PlaceGeofence, api } from "../api/client";
import type { LocationDeps } from "./LocationScreen";
import { type Px, buildProjector, heatCells } from "./locationMap";

type Mode = "live" | "trail" | "heat";

const MODE_LABEL: Record<Mode, string> = { live: "Live", trail: "Trail", heat: "Heat" };

const VIEW_W = 320;
const VIEW_H = 320;
const HEAT_CELL = 16;
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

  const [meta, setMeta] = useState<Meta>({ phase: "loading" });
  const [selected, setSelected] = useState<string>("");
  const [mode, setMode] = useState<Mode>("trail");
  const [since, setSince] = useState(() => isoDay(-DEFAULT_DAYS));
  const [until, setUntil] = useState(() => isoDay(0));
  const [fixes, setFixes] = useState<Fixes>({ phase: "loading" });

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
      <div className="loc-map-controls">
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
      </div>

      <MapCanvas mode={mode} fixes={points} places={meta.places} />

      {fixes.phase === "loading" && <p className="analysis-quiet loc-map-note">loading fixes…</p>}
      {fixes.phase === "error" && (
        <p className="analysis-quiet loc-map-note">couldn't load fixes for this range.</p>
      )}
      {fixes.phase === "done" && points.length === 0 && (
        <p className="analysis-quiet loc-map-note">no fixes in this range.</p>
      )}
    </div>
  );
}

function MapCanvas({
  mode,
  fixes,
  places,
}: {
  mode: Mode;
  fixes: LocationFix[];
  places: PlaceGeofence[];
}) {
  // Project over every drawable point so fixes and fences share one frame.
  const all = [
    ...fixes.map((f) => ({ lat: f.latitude, lon: f.longitude })),
    ...places.flatMap((p) => [...(p.center ? [p.center] : []), ...(p.polygon ?? [])]),
  ];
  const { project, metersPerPixel } = buildProjector(all, VIEW_W, VIEW_H);
  const fixPx = fixes.map((f) => project({ lat: f.latitude, lon: f.longitude }));
  const first = fixPx[0];
  const last = fixPx[fixPx.length - 1];

  return (
    <svg
      className="loc-map-svg"
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      role="img"
      aria-label="Location map"
      preserveAspectRatio="xMidYMid meet"
    >
      <rect x={0} y={0} width={VIEW_W} height={VIEW_H} className="loc-map-bg" />

      {/* Fences as context in every mode. */}
      {places.map((p) => (
        <Fence key={p.place_entity_id} place={p} project={project} mpp={metersPerPixel} />
      ))}

      {mode === "heat" &&
        heatCells(fixPx, HEAT_CELL).map((c) => (
          <rect
            key={`${c.x},${c.y}`}
            x={c.x}
            y={c.y}
            width={c.size}
            height={c.size}
            className="loc-map-heat"
            // Floor the opacity so even a single-visit cell is visible.
            fillOpacity={0.15 + 0.75 * c.weight}
          />
        ))}

      {mode === "trail" && fixPx.length > 1 && (
        <polyline className="loc-map-trail" points={fixPx.map((p) => `${p.x},${p.y}`).join(" ")} />
      )}
      {mode === "trail" && first && last && (
        <>
          <circle className="loc-map-start" cx={first.x} cy={first.y} r={4} />
          <circle className="loc-map-end" cx={last.x} cy={last.y} r={4} />
        </>
      )}

      {mode === "live" && last && <circle className="loc-map-live" cx={last.x} cy={last.y} r={6} />}
    </svg>
  );
}

function Fence({
  place,
  project,
  mpp,
}: {
  place: PlaceGeofence;
  project: (p: { lat: number; lon: number }) => Px;
  mpp: number;
}) {
  if (place.polygon && place.polygon.length > 0) {
    const pts = place.polygon.map((c) => project(c)).map((p) => `${p.x},${p.y}`);
    return (
      <polygon className="loc-map-fence" points={pts.join(" ")}>
        <title>{place.name}</title>
      </polygon>
    );
  }
  if (place.center && place.radius_m !== null) {
    const c = project(place.center);
    return (
      <circle className="loc-map-fence" cx={c.x} cy={c.y} r={Math.max(3, place.radius_m / mpp)}>
        <title>{place.name}</title>
      </circle>
    );
  }
  return null;
}
