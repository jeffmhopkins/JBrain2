// The family member's dashboard (JBrain360) — a standalone, location-only surface
// served at /dash and loaded inside the forked app's WebView. The device key lives
// in the Android Keystore and is exchanged for the session cookie natively (POST
// /api/session/mint), so this app never holds it: it probes the cookie's principal,
// and a member (device-key) session unlocks a full-screen live map scoped to its own
// + its family group.
//
// Reference mock: docs/mocks/app-live-map-v2.html. The map is the whole surface; a
// floating switcher selects a person (centering the map on them), and the bottom is a
// slim persistent bar with two pull-up sheets, one at a time: Details (the person's
// last-actions timeline / the roster) and History (Trail/Heat over a drag-both-ends
// time window). Location domain stays on --location (teal); names + times only.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  type LocationFix,
  type MemberSubject,
  type PlaceGeofence,
  type Principal,
  type TimelineEntry,
  api,
} from "../api/client";
import {
  type LocationMapHandle,
  type MapMode,
  type MapPin,
  type TileScheme,
  createLocationMap,
  readTileScheme,
  writeTileScheme,
} from "./leafletMap";
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
// Which bottom sheet is pulled up (null = collapsed, map-first).
type Sheet = null | "details" | "history";
// The time window as two slider positions 0..100 (0 = MAX_DAYS ago, 100 = now).
type Win = { from: number; to: number };

const PIN_PALETTE = 6; // loc-pin-c0..c5
const STALE_MS = 10 * 60_000; // a fix older than this reads as "stale", not "live"
const HEAT_RADIUS = 22; // px per-point heat spot — a sensible fixed value for the app
const MAX_DAYS = 7; // the window's full span (now → 7 days ago)
const DEFAULT_WIN: Win = { from: 57, to: 100 }; // ≈ last 3 days

function paletteClass(index: number): string {
  return `loc-pin-c${index % PIN_PALETTE}`;
}

function isLive(iso: string | null): boolean {
  return iso !== null && Date.now() - new Date(iso).getTime() < STALE_MS;
}

/** A window position (0..100) to an absolute epoch-ms, anchored at the call's "now". */
function posToMs(p: number, now: number): number {
  return now - ((100 - p) / 100) * MAX_DAYS * 86_400_000;
}

function winToMs(win: Win): { sinceMs: number; untilMs: number } {
  const now = Date.now();
  return {
    sinceMs: posToMs(Math.min(win.from, win.to), now),
    untilMs: posToMs(Math.max(win.from, win.to), now),
  };
}

/** A window position as a relative label ("now" / "3h ago" / "5d ago"). */
function fmtPos(p: number): string {
  const d = ((100 - p) / 100) * MAX_DAYS;
  if (d < 0.04) return "now";
  if (d < 1) return `${Math.round(d * 24)}h ago`;
  return `${d < 3 ? d.toFixed(1) : Math.round(d)}d ago`;
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
  const [win, setWin] = useState<Win>(DEFAULT_WIN);
  const [sheet, setSheet] = useState<Sheet>(null);
  // Basemap style — a tiles-only toggle (the app's dark chrome is unchanged),
  // persisted so it sticks across app launches.
  const [tileScheme, setTileScheme] = useState<TileScheme>(() => readTileScheme());

  const canvas = useRef<HTMLDivElement>(null);
  const handle = useRef<LocationMapHandle | null>(null);
  const selRef = useRef<Selection>(sel);
  selRef.current = sel;
  const rosterRef = useRef<MemberSubject[] | null>(roster);
  rosterRef.current = roster;

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
  // feed (the Details sheet filters it per person + window).
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

  // The focused person's trail over the time window (Everyone → no trail).
  useEffect(() => {
    if (sel === "all") {
      setTrail([]);
      return;
    }
    let stale = false;
    const { sinceMs, untilMs } = winToMs(win);
    listPositions(sel, new Date(sinceMs).toISOString(), new Date(untilMs).toISOString())
      .then((fixes) => {
        if (!stale) setTrail(fixes);
      })
      .catch(() => {
        if (!stale) setTrail([]);
      });
    return () => {
      stale = true;
    };
  }, [sel, win, listPositions]);

  // Selecting a person recenters the map on them (their current pin at select time).
  useEffect(() => {
    if (sel === "all") return;
    const p = rosterRef.current?.find((s) => s.subject_id === sel);
    if (p?.latitude != null && p?.longitude != null)
      handle.current?.centerOn(p.latitude, p.longitude);
  }, [sel]);

  // History has no meaning for Everyone — collapse it on the way there.
  useEffect(() => {
    if (sel === "all") setSheet((s) => (s === "history" ? null : s));
  }, [sel]);

  // Each visible subject gets a stable colour by roster order.
  const colorOf = useMemo(() => {
    const m = new Map<string, string>();
    (roster ?? []).forEach((s, i) => m.set(s.subject_id, paletteClass(i)));
    return m;
  }, [roster]);

  // Drive the map: Everyone → all current pins (auto-fit); a focus → that person's
  // pin plus their trail/heat, with the view owned by centerOn (no auto-fit).
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
      autoFit: sel === "all",
    });
  }, [roster, places, sel, colorOf, mode, trail]);

  // Swap the basemap in place when the toggle changes (no remount; pins stay put).
  useEffect(() => {
    handle.current?.setScheme(tileScheme);
  }, [tileScheme]);

  const toggleSheet = (which: Exclude<Sheet, null>) =>
    setSheet((s) => (s === which ? null : which));

  const pickScheme = (s: TileScheme) => {
    setTileScheme(s);
    writeTileScheme(s);
  };

  return (
    <div className="livemap">
      <div className="livemap-canvas" ref={canvas} data-testid="map-canvas" />
      <PeopleSwitcher roster={roster} sel={sel} colorOf={colorOf} failed={failed} onPick={setSel} />
      <TileToggle scheme={tileScheme} onPick={pickScheme} />
      <DetailsSheet
        open={sheet === "details"}
        roster={roster}
        sel={sel}
        colorOf={colorOf}
        timeline={timeline}
        win={win}
        onPick={setSel}
        onClose={() => setSheet(null)}
      />
      <HistorySheet
        open={sheet === "history"}
        mode={mode}
        win={win}
        onMode={setMode}
        onWin={setWin}
        onClose={() => setSheet(null)}
      />
      <DockBar roster={roster} sel={sel} colorOf={colorOf} sheet={sheet} onToggle={toggleSheet} />
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

// --- basemap light/dark toggle (tiles only) -------------------------------

/** A compact floating control that switches ONLY the basemap tiles between the
 * dark and light schemes — the app's own dark chrome is untouched. */
function TileToggle({
  scheme,
  onPick,
}: {
  scheme: TileScheme;
  onPick: (s: TileScheme) => void;
}) {
  return (
    <div className="lm-tiles lm-float" role="tablist" aria-label="Map style">
      <button
        type="button"
        role="tab"
        className={scheme === "dark" ? "on" : ""}
        aria-selected={scheme === "dark"}
        aria-label="Dark map"
        title="Dark map"
        onClick={() => onPick("dark")}
      >
        ☾
      </button>
      <button
        type="button"
        role="tab"
        className={scheme === "light" ? "on" : ""}
        aria-selected={scheme === "light"}
        aria-label="Light map"
        title="Light map"
        onClick={() => onPick("light")}
      >
        ☀
      </button>
    </div>
  );
}

// --- bottom dock: a persistent bar + two pull-up sheets -------------------

function DockBar({
  roster,
  sel,
  colorOf,
  sheet,
  onToggle,
}: {
  roster: MemberSubject[] | null;
  sel: Selection;
  colorOf: Map<string, string>;
  sheet: Sheet;
  onToggle: (which: Exclude<Sheet, null>) => void;
}) {
  if (!roster) return null;
  if (roster.length === 0) {
    return <div className="lm-bar lm-float lm-bar-quiet">no one to show yet.</div>;
  }
  const everyone = sel === "all";
  const m = everyone ? null : roster.find((s) => s.subject_id === sel);
  return (
    <div className="lm-bar lm-float">
      <div className="lm-bar-who">
        {everyone ? (
          <>
            <span className="lm-av lm-av-all sm" aria-hidden>
              ◎
            </span>
            <span className="lm-bar-txt">
              <span className="lm-bar-nm">Everyone</span>
              <span className="lm-bar-sub">{roster.length} family members</span>
            </span>
          </>
        ) : m ? (
          <>
            <span className={`lm-av sm ${colorOf.get(m.subject_id) ?? "loc-pin-c0"}`}>
              {(m.label[0] ?? "?").toUpperCase()}
            </span>
            <span className="lm-bar-txt">
              <span className="lm-bar-nm">{m.label}</span>
              <span className="lm-bar-sub">
                <PresenceDot live={isLive(m.last_seen)} /> {presence(m)}
              </span>
            </span>
          </>
        ) : null}
      </div>
      <button
        type="button"
        className={`lm-pull${sheet === "details" ? " on" : ""}`}
        aria-expanded={sheet === "details"}
        onClick={() => onToggle("details")}
      >
        <PullChevron />
        Details
      </button>
      <button
        type="button"
        className={`lm-pull${sheet === "history" ? " on" : ""}`}
        aria-expanded={sheet === "history"}
        disabled={everyone}
        onClick={() => onToggle("history")}
      >
        <PullChevron />
        History
      </button>
    </div>
  );
}

function DetailsSheet({
  open,
  roster,
  sel,
  colorOf,
  timeline,
  win,
  onPick,
  onClose,
}: {
  open: boolean;
  roster: MemberSubject[] | null;
  sel: Selection;
  colorOf: Map<string, string>;
  timeline: TimelineEntry[];
  win: Win;
  onPick: (s: Selection) => void;
  onClose: () => void;
}) {
  if (!roster) return null;
  return (
    <div className={`lm-sheet lm-float${open ? " open" : ""}`} aria-hidden={!open}>
      <button type="button" className="lm-grab" aria-label="Collapse" onClick={onClose} />
      {sel === "all" ? (
        <RosterList roster={roster} colorOf={colorOf} onPick={onPick} />
      ) : (
        <PersonActivity roster={roster} sel={sel} timeline={timeline} win={win} />
      )}
      <PrivacyLine />
    </div>
  );
}

function RosterList({
  roster,
  colorOf,
  onPick,
}: {
  roster: MemberSubject[];
  colorOf: Map<string, string>;
  onPick: (s: Selection) => void;
}) {
  return (
    <>
      <div className="lm-sec-h">Everyone · {roster.length}</div>
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
    </>
  );
}

function PersonActivity({
  roster,
  sel,
  timeline,
  win,
}: {
  roster: MemberSubject[];
  sel: Selection;
  timeline: TimelineEntry[];
  win: Win;
}) {
  const m = roster.find((s) => s.subject_id === sel);
  if (!m) return null;
  const actions = recentActions(timeline, m.subject_id, win);
  return (
    <>
      <div className="lm-sec-h">{m.label} · recent activity</div>
      {actions.length === 0 ? (
        <div className="lm-card-quiet">no arrivals or departures in this window.</div>
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
    </>
  );
}

function HistorySheet({
  open,
  mode,
  win,
  onMode,
  onWin,
  onClose,
}: {
  open: boolean;
  mode: Exclude<MapMode, "live">;
  win: Win;
  onMode: (m: Exclude<MapMode, "live">) => void;
  onWin: (w: Win) => void;
  onClose: () => void;
}) {
  return (
    <div className={`lm-sheet lm-float${open ? " open" : ""}`} aria-hidden={!open}>
      <button type="button" className="lm-grab" aria-label="Collapse" onClick={onClose} />
      <div className="lm-sec-h">Show</div>
      <div className="lm-seg">
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
      <div className="lm-sec-h">Time window</div>
      <TimeWindow win={win} onWin={onWin} />
      <div className="lm-priv">Family-only · names + times only, never a coordinate.</div>
    </div>
  );
}

function TimeWindow({ win, onWin }: { win: Win; onWin: (w: Win) => void }) {
  const lo = Math.min(win.from, win.to);
  const hi = Math.max(win.from, win.to);
  return (
    <div className="lm-win">
      <div className="lm-winlbl">
        <span>
          From <b>{fmtPos(lo)}</b>
        </span>
        <span>
          to <b>{fmtPos(hi)}</b>
        </span>
      </div>
      <div className="lm-range">
        <div className="lm-range-track" />
        <div className="lm-range-fill" style={{ left: `${lo}%`, width: `${hi - lo}%` }} />
        <input
          type="range"
          min={0}
          max={100}
          value={win.from}
          aria-label="Window start"
          onChange={(e) => onWin({ ...win, from: Number(e.target.value) })}
        />
        <input
          type="range"
          min={0}
          max={100}
          value={win.to}
          aria-label="Window end"
          onChange={(e) => onWin({ ...win, to: Number(e.target.value) })}
        />
      </div>
      <div className="lm-ticks">
        <span>7d</span>
        <span>5d</span>
        <span>3d</span>
        <span>1d</span>
        <span>now</span>
      </div>
    </div>
  );
}

function PullChevron() {
  return (
    <svg className="lm-pull-ic" viewBox="0 0 24 24" aria-hidden="true" role="img">
      <title>expand</title>
      <path
        d="M6 15l6-6 6 6"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

// --- last-actions helpers --------------------------------------------------

/** The selected person's geofence crossings within the time window (newest first) —
 * names + times only, never a coordinate. */
function recentActions(timeline: TimelineEntry[], subjectId: string, win: Win): TimelineEntry[] {
  const { sinceMs, untilMs } = winToMs(win);
  return timeline
    .filter((e) => {
      if (e.subject_id !== subjectId) return false;
      const t = new Date(e.occurred_at).getTime();
      return t >= sinceMs && t <= untilMs;
    })
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

/** A compact "last fix" relative time for the roster/bar (never an exact position —
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
