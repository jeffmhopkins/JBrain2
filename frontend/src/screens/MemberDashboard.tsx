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
import {
  type LocationFix,
  type MemberSubject,
  type PlaceGeofence,
  type Principal,
  type TimelineEntry,
  api,
} from "../api/client";
import { type LocationMapHandle, type MapMode, type MapPin, createLocationMap } from "./leafletMap";
import { type LiveFix, connectLive } from "./liveSocket";

export interface MemberDeps {
  /** Resolve the session cookie's principal; rejects (401) when unauthenticated. */
  probe: () => Promise<Principal>;
  listRoster: () => Promise<MemberSubject[]>;
  listPlaces: () => Promise<PlaceGeofence[]>;
  listPositions: (subjectId: string, since: string, until: string) => Promise<LocationFix[]>;
  listTimeline: () => Promise<TimelineEntry[]>;
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

const HEAT_RADIUS = 22; // px per-point heat spot — a sensible fixed value for the app
const MAX_DAYS = 7;

function windowIso(days: number): { since: string; until: string } {
  const until = new Date();
  const since = new Date(until.getTime() - days * 86_400_000);
  return { since: since.toISOString(), until: until.toISOString() };
}

function LiveMap({ deps }: { deps: MemberDeps | undefined }) {
  const listRoster = deps?.listRoster ?? api.memberRoster;
  const listPlaces = deps?.listPlaces ?? api.memberPlaces;
  const listPositions = deps?.listPositions ?? api.memberPositions;
  const listTimeline = deps?.listTimeline ?? api.memberTimeline;

  const [roster, setRoster] = useState<MemberSubject[] | null>(null);
  const [places, setPlaces] = useState<PlaceGeofence[]>([]);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [trail, setTrail] = useState<LocationFix[]>([]);
  const [failed, setFailed] = useState(false);
  const [sel, setSel] = useState<Selection>("all");
  const [mode, setMode] = useState<Exclude<MapMode, "live">>("trail");
  const [days, setDays] = useState(3);
  const [cardOpen, setCardOpen] = useState(false);

  const canvas = useRef<HTMLDivElement>(null);
  const handle = useRef<LocationMapHandle | null>(null);
  const selRef = useRef<Selection>(sel);
  selRef.current = sel;

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

  // Roster (with each subject's latest coordinate) + shared fences + the transition
  // feed (the last-actions card filters it per person).
  useEffect(() => {
    let stale = false;
    Promise.all([listRoster(), listPlaces(), listTimeline()])
      .then(([r, p, t]) => {
        if (stale) return;
        setRoster(r);
        setPlaces(p);
        setTimeline(t);
      })
      .catch(() => {
        if (!stale) setFailed(true);
      });
    return () => {
      stale = true;
    };
  }, [listRoster, listPlaces, listTimeline]);

  // Live fixes move each visible person's pin (so the map is live) and extend the
  // focused person's trail. The server already scopes the stream to self + group.
  useEffect(() => {
    const live = connectLive((fix: LiveFix) => {
      setRoster(
        (prev) =>
          prev?.map((s) =>
            s.subject_id === fix.subject_id
              ? {
                  ...s,
                  latitude: fix.lat,
                  longitude: fix.lon,
                  last_seen: fix.captured_at,
                  battery_pct: fix.battery_pct ?? s.battery_pct,
                }
              : s,
          ) ?? prev,
      );
      if (fix.subject_id === selRef.current) {
        setTrail((t) => [
          ...t,
          {
            captured_at: fix.captured_at,
            latitude: fix.lat,
            longitude: fix.lon,
            accuracy_m: fix.accuracy_m,
            battery_pct: fix.battery_pct,
          },
        ]);
      }
    });
    return () => live.close();
  }, []);

  // The focused person's trail over the chosen range (Everyone → no trail).
  useEffect(() => {
    if (sel === "all") {
      setTrail([]);
      return;
    }
    let stale = false;
    const { since, until } = windowIso(days);
    listPositions(sel, since, until)
      .then((fixes) => {
        if (!stale) setTrail(fixes);
      })
      .catch(() => {
        if (!stale) setTrail([]);
      });
    return () => {
      stale = true;
    };
  }, [sel, days, listPositions]);

  // Collapse the expanded card whenever the focus changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: collapse on focus change
  useEffect(() => setCardOpen(false), [sel]);

  // Each visible subject gets a stable colour by roster order.
  const colorOf = useMemo(() => {
    const m = new Map<string, string>();
    (roster ?? []).forEach((s, i) => m.set(s.subject_id, paletteClass(i)));
    return m;
  }, [roster]);

  // Drive the map: Everyone → all current pins (no trail); a focus → that person's
  // pin plus their trail or heat over the range.
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
    h.update({
      mode: sel === "all" ? "live" : mode,
      fixes: sel === "all" ? [] : trail,
      heatRadius: HEAT_RADIUS,
      places,
      pins,
    });
  }, [roster, places, sel, colorOf, mode, trail]);

  return (
    <div className="livemap">
      <div className="livemap-canvas" ref={canvas} data-testid="map-canvas" />
      <PeopleSwitcher roster={roster} sel={sel} colorOf={colorOf} failed={failed} onPick={setSel} />
      {sel !== "all" && <MapControls mode={mode} days={days} onMode={setMode} onDays={setDays} />}
      <PersonCard
        roster={roster}
        sel={sel}
        colorOf={colorOf}
        onPick={setSel}
        timeline={timeline}
        days={days}
        open={cardOpen}
        onToggle={() => setCardOpen((o) => !o)}
      />
    </div>
  );
}

// --- floating mode + range controls (single-person only) ------------------

function MapControls({
  mode,
  days,
  onMode,
  onDays,
}: {
  mode: Exclude<MapMode, "live">;
  days: number;
  onMode: (m: Exclude<MapMode, "live">) => void;
  onDays: (d: number) => void;
}) {
  return (
    <div className="lm-ctrls lm-float">
      {/* biome-ignore lint/a11y/useSemanticElements: a 2-button toggle group; a <fieldset> is overkill */}
      <div className="lm-seg" role="group" aria-label="Overlay">
        <button
          type="button"
          className={mode === "trail" ? "on" : ""}
          aria-pressed={mode === "trail"}
          onClick={() => onMode("trail")}
        >
          Trail
        </button>
        <button
          type="button"
          className={mode === "heat" ? "on" : ""}
          aria-pressed={mode === "heat"}
          onClick={() => onMode("heat")}
        >
          Heat
        </button>
      </div>
      <label className="lm-range">
        <span className="lm-range-lbl">
          Last <b>{days}</b> day{days > 1 ? "s" : ""}
        </span>
        <input
          type="range"
          min={1}
          max={MAX_DAYS}
          value={days}
          aria-label="Days of history"
          onChange={(e) => onDays(Number(e.target.value))}
        />
      </label>
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
  timeline,
  days,
  open,
  onToggle,
}: {
  roster: MemberSubject[] | null;
  sel: Selection;
  colorOf: Map<string, string>;
  onPick: (s: Selection) => void;
  timeline: TimelineEntry[];
  days: number;
  open: boolean;
  onToggle: () => void;
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
  const actions = recentActions(timeline, m.subject_id, days);
  return (
    <div className={`lm-card lm-float${open ? " open" : ""}`}>
      <button type="button" className="lm-chead" onClick={onToggle} aria-expanded={open}>
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
        <span className="lm-chev" aria-hidden>
          ⌃
        </span>
      </button>
      {actions.length > 0 && (
        <div className="lm-quick">
          {actions.slice(0, 4).map((e, i) => (
            <span key={`${e.occurred_at}-${i}`} className="lm-qpill">
              <span className={e.transition === "enter" ? "in" : "out"}>●</span>{" "}
              {timeOfDay(e.occurred_at)} {actionText(e)}
            </span>
          ))}
        </div>
      )}
      {open && (
        <div className="lm-more">
          <div className="lm-more-h">
            Recent activity · last {days} day{days > 1 ? "s" : ""}
          </div>
          {actions.length === 0 ? (
            <div className="lm-card-quiet">no arrivals or departures yet.</div>
          ) : (
            <div className="lm-tl">
              {groupActionsByDay(actions).map((g) => (
                <section key={g.day}>
                  {g.rows.map((e, i) => (
                    <div
                      key={`${e.occurred_at}-${i}`}
                      className={`lm-ev ${e.transition === "enter" ? "in" : "out"}`}
                    >
                      <span className="lm-ev-mk" aria-hidden />
                      <span className="lm-ev-t">{timeOfDay(e.occurred_at)}</span>
                      <span className="lm-ev-d">
                        {actionText(e)}
                        {i === 0 && <span className="lm-ev-day"> · {g.day}</span>}
                      </span>
                    </div>
                  ))}
                </section>
              ))}
            </div>
          )}
        </div>
      )}
      <PrivacyLine />
    </div>
  );
}

/** The selected person's recent geofence crossings (newest first), within the chosen
 * day range — names + times only, never a coordinate. */
function recentActions(
  timeline: TimelineEntry[],
  subjectId: string,
  days: number,
): TimelineEntry[] {
  const floor = Date.now() - days * 86_400_000;
  return timeline
    .filter((e) => e.subject_id === subjectId && new Date(e.occurred_at).getTime() >= floor)
    .sort((a, b) => new Date(b.occurred_at).getTime() - new Date(a.occurred_at).getTime());
}

function actionText(e: TimelineEntry): string {
  return `${e.transition === "enter" ? "Arrived" : "Left"} ${e.place_name}`;
}

function groupActionsByDay(actions: TimelineEntry[]): { day: string; rows: TimelineEntry[] }[] {
  const out: { day: string; rows: TimelineEntry[] }[] = [];
  for (const e of actions) {
    const day = dayLabel(e.occurred_at);
    const last = out[out.length - 1];
    if (last && last.day === day) last.rows.push(e);
    else out.push({ day, rows: [e] });
  }
  return out;
}

function timeOfDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Unknown";
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  const same = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
  if (same(d, today)) return "Today";
  if (same(d, yesterday)) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
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
