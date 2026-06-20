// The family member's dashboard (JBrain360) — a standalone, location-only surface
// served at /dash and loaded inside the forked app's WebView. The device key lives
// in the Android Keystore and is exchanged for the session cookie natively (POST
// /api/session/mint), so this app never holds it: it probes the cookie's principal,
// and a member (device-key) session unlocks a full-screen live map scoped to its own
// + its family group.
//
// Wave 2 (docs/PHASE7_APP_MAP_PLAN.md) ships the full-screen map shell: the floating
// person switcher, current-location pins, and the bottom status/roster card. Trail /
// heat / the 1–7 day range / the last-actions timeline land in Wave 3. Reference
// mock: docs/mocks/app-live-map.html. Location domain stays on --location (teal).

import { useEffect, useMemo, useRef, useState } from "react";
import { type MemberSubject, type PlaceGeofence, type Principal, api } from "../api/client";
import { type LocationMapHandle, type MapPin, createLocationMap } from "./leafletMap";

export interface MemberDeps {
  /** Resolve the session cookie's principal; rejects (401) when unauthenticated. */
  probe: () => Promise<Principal>;
  listRoster: () => Promise<MemberSubject[]>;
  listPlaces: () => Promise<PlaceGeofence[]>;
}

type Gate = { phase: "probing" } | { phase: "locked" } | { phase: "ready" };

interface MemberDashboardProps {
  /** Injectable for tests; defaults to the live API client. */
  deps?: MemberDeps;
}

export function MemberDashboard({ deps }: MemberDashboardProps) {
  const probe = deps?.probe ?? api.me;
  const [gate, setGate] = useState<Gate>({ phase: "probing" });

  useEffect(() => {
    let stale = false;
    probe()
      .then((p) => {
        if (stale) return;
        // Only a device-key cookie is a member session; an owner (or anything else)
        // belongs on the main app, not here.
        setGate(p.kind === "device_key" ? { phase: "ready" } : { phase: "locked" });
      })
      .catch(() => {
        if (!stale) setGate({ phase: "locked" });
      });
    return () => {
      stale = true;
    };
  }, [probe]);

  if (gate.phase === "probing") {
    return <main className="dash-frame dash-center dash-quiet">checking your session…</main>;
  }
  if (gate.phase === "locked") {
    return (
      <main className="dash-frame dash-center dash-quiet">
        not signed in — open JBrain360 from the app to view the family map.
      </main>
    );
  }
  return <LiveMap deps={deps} />;
}

// --- the full-screen live map ---------------------------------------------

// "all" shows everyone's current pins; a subject id focuses one person.
type Selection = "all" | string;

const PIN_PALETTE = 6; // loc-pin-c0..c5
const STALE_MS = 10 * 60_000; // a fix older than this reads as "stale", not "live"

function paletteClass(index: number): string {
  return `loc-pin-c${index % PIN_PALETTE}`;
}

function isLive(iso: string | null): boolean {
  return iso !== null && Date.now() - new Date(iso).getTime() < STALE_MS;
}

function LiveMap({ deps }: { deps: MemberDeps | undefined }) {
  const listRoster = deps?.listRoster ?? api.memberRoster;
  const listPlaces = deps?.listPlaces ?? api.memberPlaces;

  const [roster, setRoster] = useState<MemberSubject[] | null>(null);
  const [places, setPlaces] = useState<PlaceGeofence[]>([]);
  const [failed, setFailed] = useState(false);
  const [sel, setSel] = useState<Selection>("all");

  const canvas = useRef<HTMLDivElement>(null);
  const handle = useRef<LocationMapHandle | null>(null);

  // Create the Leaflet map once; a pin tap follows through to the switcher.
  useEffect(() => {
    if (!canvas.current) return;
    const h = createLocationMap(canvas.current, (id) => setSel(id));
    handle.current = h;
    return () => {
      h.destroy();
      handle.current = null;
    };
  }, []);

  // Load the roster (which now carries each visible subject's coordinate) + the
  // shared fences.
  useEffect(() => {
    let stale = false;
    Promise.all([listRoster(), listPlaces()])
      .then(([r, p]) => {
        if (stale) return;
        setRoster(r);
        setPlaces(p);
      })
      .catch(() => {
        if (!stale) setFailed(true);
      });
    return () => {
      stale = true;
    };
  }, [listRoster, listPlaces]);

  // Each visible subject gets a stable colour by roster order.
  const colorOf = useMemo(() => {
    const m = new Map<string, string>();
    (roster ?? []).forEach((s, i) => m.set(s.subject_id, paletteClass(i)));
    return m;
  }, [roster]);

  // Drive the map's pins from the roster + selection: Everyone shows all located
  // people, a single selection shows just that person (so the map recenters on them).
  useEffect(() => {
    const h = handle.current;
    if (!h || !roster) return;
    const located = roster.filter((s) => s.latitude !== null && s.longitude !== null);
    const shown = sel === "all" ? located : located.filter((s) => s.subject_id === sel);
    const pins: MapPin[] = shown.map((s) => ({
      subjectId: s.subject_id,
      lat: s.latitude as number,
      lon: s.longitude as number,
      label: s.label,
      colorClass: colorOf.get(s.subject_id) ?? "loc-pin-c0",
      live: isLive(s.last_seen),
      selected: s.subject_id === sel,
    }));
    h.update({ mode: "live", fixes: [], heatRadius: 25, places, pins });
  }, [roster, places, sel, colorOf]);

  return (
    <div className="livemap">
      <div className="livemap-canvas" ref={canvas} data-testid="map-canvas" />
      <PeopleSwitcher roster={roster} sel={sel} colorOf={colorOf} failed={failed} onPick={setSel} />
      <PersonCard roster={roster} sel={sel} colorOf={colorOf} onPick={setSel} />
    </div>
  );
}

// --- floating person switcher ---------------------------------------------

function PeopleSwitcher({
  roster,
  sel,
  colorOf,
  failed,
  onPick,
}: {
  roster: MemberSubject[] | null;
  sel: Selection;
  colorOf: Map<string, string>;
  failed: boolean;
  onPick: (s: Selection) => void;
}) {
  if (failed) {
    return <div className="lm-people lm-float lm-people-quiet">couldn't load the family</div>;
  }
  if (!roster) {
    return <div className="lm-people lm-float lm-people-quiet">loading…</div>;
  }
  return (
    <div className="lm-people lm-float" role="tablist" aria-label="Family">
      <button
        type="button"
        role="tab"
        aria-selected={sel === "all"}
        className={`lm-chip${sel === "all" ? " on" : ""}`}
        onClick={() => onPick("all")}
      >
        <span className="lm-av lm-av-all" aria-hidden>
          ◎
        </span>
        <span className="lm-chip-nm">Everyone</span>
      </button>
      {roster.map((m) => (
        <button
          key={m.subject_id}
          type="button"
          role="tab"
          aria-selected={sel === m.subject_id}
          className={`lm-chip${sel === m.subject_id ? " on" : ""}`}
          onClick={() => onPick(m.subject_id)}
        >
          <span className={`lm-av ${colorOf.get(m.subject_id) ?? "loc-pin-c0"}`}>
            {(m.label[0] ?? "?").toUpperCase()}
            <span className={`lm-pres ${isLive(m.last_seen) ? "live" : "stale"}`} aria-hidden />
          </span>
          <span className="lm-chip-nm">{m.label}</span>
        </button>
      ))}
    </div>
  );
}

// --- floating bottom card -------------------------------------------------

function PersonCard({
  roster,
  sel,
  colorOf,
  onPick,
}: {
  roster: MemberSubject[] | null;
  sel: Selection;
  colorOf: Map<string, string>;
  onPick: (s: Selection) => void;
}) {
  if (!roster) return null;
  if (roster.length === 0) {
    return <div className="lm-card lm-float lm-card-quiet">no one to show yet.</div>;
  }

  if (sel === "all") {
    return (
      <div className="lm-card lm-float">
        <div className="lm-card-h">Everyone · {roster.length}</div>
        <div className="lm-roster">
          {roster.map((m) => (
            <button
              type="button"
              key={m.subject_id}
              className="lm-row"
              onClick={() => onPick(m.subject_id)}
            >
              <span className={`lm-av sm ${colorOf.get(m.subject_id) ?? "loc-pin-c0"}`}>
                {(m.label[0] ?? "?").toUpperCase()}
              </span>
              <span className="lm-row-main">
                <span className="lm-row-nm">{m.label}</span>
                <span className="lm-row-sub">
                  <PresenceDot live={isLive(m.last_seen)} /> {presence(m)}
                </span>
              </span>
              <Battery pct={m.battery_pct} />
            </button>
          ))}
        </div>
        <PrivacyLine />
      </div>
    );
  }

  const m = roster.find((s) => s.subject_id === sel);
  if (!m) return null;
  return (
    <div className="lm-card lm-float">
      <div className="lm-chead">
        <span className={`lm-av ${colorOf.get(m.subject_id) ?? "loc-pin-c0"}`}>
          {(m.label[0] ?? "?").toUpperCase()}
        </span>
        <span className="lm-chead-main">
          <span className="lm-chead-nm">{m.label}</span>
          <span className="lm-chead-st">
            <PresenceDot live={isLive(m.last_seen)} /> {presence(m)}
          </span>
        </span>
        <Battery pct={m.battery_pct} />
      </div>
      <PrivacyLine />
    </div>
  );
}

function presence(m: MemberSubject): string {
  if (!m.last_seen) return "no fixes yet";
  return `${isLive(m.last_seen) ? "Live" : "Last seen"} · ${lastSeen(m.last_seen)}`;
}

function PresenceDot({ live }: { live: boolean }) {
  return <span className={`lm-dot ${live ? "live" : "stale"}`} aria-hidden />;
}

function Battery({ pct }: { pct: number | null }) {
  if (pct === null) return null;
  return (
    <span className="lm-batt" title={`battery ${pct}%`}>
      <span className="lm-batt-cell">
        <span className={`lm-batt-fill${pct < 25 ? " low" : ""}`} style={{ width: `${pct}%` }} />
      </span>
      {pct}%
    </span>
  );
}

function PrivacyLine() {
  return <div className="lm-priv">Family-only · history stays on your box</div>;
}

/** A compact "last fix" relative time for the roster/card (never an exact position —
 * just freshness, so a stale dot is never read as "here now"). */
export function lastSeen(iso: string | null): string {
  if (!iso) return "no fixes yet";
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
