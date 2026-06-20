// The Map tab of the member dashboard (JBrain360 M4d-2c): a Leaflet basemap over
// the server-side tile proxy, showing one visible subject's recent trail — growing
// live off the scope-filtered WebSocket — with the owner-shared fences as context.
//
// Simpler than the owner map by design: no date picker, no Heat/edit modes, no
// reverse-geocode. Reads /api/member/{roster,positions,places} and the shared
// /api/locations/live feed (the server filters it to this member's own + group).
// Leaflet is isolated in leafletMap.ts and the socket in liveSocket.ts.

import { useEffect, useRef, useState } from "react";
import { type LocationFix, type PlaceGeofence, api } from "../api/client";
import { type MemberDeps, lastSeen } from "./MemberDashboard";
import { type LocationMapHandle, createLocationMap } from "./leafletMap";
import { type LiveFix, connectLive } from "./liveSocket";

// The trail window: the last day of positions for the selected subject. The live
// socket extends it as fresh fixes arrive.
const TRAIL_HOURS = 24;

type Meta =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "ready"; roster: RosterRow[]; places: PlaceGeofence[] };

type RosterRow = { subject_id: string; label: string };

type Fixes = { phase: "loading" } | { phase: "error" } | { phase: "done"; fixes: LocationFix[] };

function liveToFix(f: LiveFix): LocationFix {
  return {
    captured_at: f.captured_at,
    latitude: f.lat,
    longitude: f.lon,
    accuracy_m: f.accuracy_m,
    battery_pct: f.battery_pct,
  };
}

export function MemberMapTab({ deps }: { deps: MemberDeps | undefined }) {
  const listRoster = deps?.listRoster ?? api.memberRoster;
  const listPlaces = deps?.listPlaces ?? api.memberPlaces;
  const listPositions = deps?.listPositions ?? api.memberPositions;

  const [meta, setMeta] = useState<Meta>({ phase: "loading" });
  const [selected, setSelected] = useState<string>("");
  const [fixes, setFixes] = useState<Fixes>({ phase: "loading" });
  // The live handler closes over the latest selection without reconnecting.
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  useEffect(() => {
    let stale = false;
    Promise.all([listRoster(), listPlaces()])
      .then(([roster, places]) => {
        if (stale) return;
        setMeta({ phase: "ready", roster, places });
        setSelected((s) => s || roster[0]?.subject_id || "");
      })
      .catch(() => {
        if (!stale) setMeta({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [listRoster, listPlaces]);

  useEffect(() => {
    if (!selected) return;
    let stale = false;
    setFixes({ phase: "loading" });
    const until = new Date();
    const since = new Date(until.getTime() - TRAIL_HOURS * 3_600_000);
    listPositions(selected, since.toISOString(), until.toISOString())
      .then((rows) => {
        if (!stale) setFixes({ phase: "done", fixes: rows });
      })
      .catch(() => {
        if (!stale) setFixes({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [selected, listPositions]);

  // One live connection for the tab's life: append a fix to the trail when it's
  // for the currently selected subject (the server already scoped the stream).
  useEffect(() => {
    const handle = connectLive((f) => {
      if (f.subject_id !== selectedRef.current) return;
      setFixes((prev) =>
        prev.phase === "done" ? { phase: "done", fixes: [...prev.fixes, liveToFix(f)] } : prev,
      );
    });
    return () => handle.close();
  }, []);

  if (meta.phase === "loading") return <p className="dash-quiet dash-pad">loading map…</p>;
  if (meta.phase === "error") {
    return <p className="dash-quiet dash-pad">couldn't load the map — check the connection.</p>;
  }
  if (meta.roster.length === 0) {
    return <p className="dash-quiet dash-pad">no one to show yet.</p>;
  }

  const points = fixes.phase === "done" ? fixes.fixes : [];
  const latest = points[points.length - 1];

  return (
    <div className="loc-map">
      <div className="loc-map-stage">
        <MemberLeaflet fixes={points} places={meta.places} />
        <div className="loc-map-overlay loc-map-overlay-top">
          {meta.roster.length > 1 && (
            <select
              className="loc-map-device"
              aria-label="Person"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              {meta.roster.map((r) => (
                <option key={r.subject_id} value={r.subject_id}>
                  {r.label}
                </option>
              ))}
            </select>
          )}
          {latest && <span className="dash-quiet">updated {lastSeen(latest.captured_at)}</span>}
        </div>
      </div>
    </div>
  );
}

function MemberLeaflet({ fixes, places }: { fixes: LocationFix[]; places: PlaceGeofence[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const handle = useRef<LocationMapHandle | null>(null);

  // Create the map once the container is in the DOM; tear it down on unmount.
  useEffect(() => {
    if (!ref.current) return;
    handle.current = createLocationMap(ref.current);
    return () => {
      handle.current?.destroy();
      handle.current = null;
    };
  }, []);

  // Redraw the trail + shared fences whenever the data changes. "trail" draws the
  // path with start/end markers; a live fix moves the end marker.
  useEffect(() => {
    handle.current?.update({ mode: "trail", fixes, places, heatRadius: 25 });
  }, [fixes, places]);

  return <div ref={ref} className="loc-map-canvas" aria-label="Family map" />;
}
