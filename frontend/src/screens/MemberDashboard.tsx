// The family member's dashboard (JBrain360) — a standalone, location-only surface
// served at /dash and loaded inside the forked app's WebView. The device key lives
// in the Android Keystore and is exchanged for the session cookie natively (POST
// /api/session/mint), so this app never holds it: it probes the cookie's principal,
// and a member (device-key) session unlocks a full-screen live map scoped to its own
// + its family group.
//
// Reference mock: docs/mocks/app-live-map-v2.html. The map is the whole surface; a
// floating switcher selects a person (centering the map on them), and the bottom is a
// slim persistent bar with two pull-up sheets, one at a time: tapping the person area
// of the bar opens Details (the person's last-actions timeline / the roster); a
// History button opens Trail/Heat over a drag-both-ends time window. Location domain
// stays on --location (teal); names + times only.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  type MapState,
  type TileScheme,
  createLocationMap,
  readTileScheme,
  writeTileScheme,
} from "./leafletMap";
import { type LiveFix, connectLive } from "./liveSocket";
import { TRAVELING_MIN_MPS, travelingSpeedMph } from "./speed";
import {
  METRIC_LABEL,
  TRAIL_METRICS,
  type TrailMetric,
  colorForFix,
  computeDwell,
  legendInfo,
  metricLabel,
} from "./trailMetric";

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
// The trail/activity time window: a fixed "last N" preset (replaces the old
// drag-both-ends slider). 1h/8h/1d/7d spans "right now" through "this week" in a tap.
type WindowPreset = "1h" | "8h" | "1d" | "7d";
const WINDOW_PRESETS: WindowPreset[] = ["1h", "8h", "1d", "7d"];
const PRESET_MS: Record<WindowPreset, number> = {
  "1h": 3_600_000,
  "8h": 8 * 3_600_000,
  "1d": 86_400_000,
  "7d": 7 * 86_400_000,
};
const DEFAULT_PRESET: WindowPreset = "1d";

const PIN_PALETTE = 6; // loc-pin-c0..c5
const STALE_MS = 10 * 60_000; // a fix older than this reads as "stale", not "live"
const HEAT_RADIUS = 22; // px per-point heat spot — a sensible fixed value for the app
// Live fixes for OTHER people coalesce to this cadence so the map of others doesn't
// churn; the viewer's own device updates on every fix (snappy self-tracking).
const OTHERS_FLUSH_MS = 10_000;

function paletteClass(index: number): string {
  return `loc-pin-c${index % PIN_PALETTE}`;
}

function isLive(iso: string | null): boolean {
  return iso !== null && Date.now() - new Date(iso).getTime() < STALE_MS;
}

/** The [since, until) epoch-ms window for a "last N" preset, anchored at now. */
function presetToMs(preset: WindowPreset): { sinceMs: number; untilMs: number } {
  const now = Date.now();
  return { sinceMs: now - PRESET_MS[preset], untilMs: now };
}

// The dual slider's two ends, as 0..100 positions over the loaded trail's own time
// extent (0 = first fix, 100 = newest). It's a pure in-memory trim, never a refetch.
type ScrubWindow = { lo: number; hi: number };

/** A "current position" fix from a roster row (its latest coordinate + speed/battery),
 * for the detail card when a person is selected but their trail hasn't loaded yet.
 * Course/accel/altitude aren't on the roster, so they read as "—". */
function subjectCurrentFix(m: MemberSubject | undefined): LocationFix | null {
  if (!m || m.latitude == null || m.longitude == null) return null;
  return {
    captured_at: m.last_seen ?? new Date().toISOString(),
    latitude: m.latitude,
    longitude: m.longitude,
    accuracy_m: null,
    battery_pct: m.battery_pct,
    velocity_mps: m.velocity_mps,
    course_deg: null,
    acceleration_mps2: null,
    altitude_m: null,
  };
}

/** Trim a (time-ordered) trail to the slider's [lo, hi] fraction of its own span. */
function trimByWindow(trail: LocationFix[], win: ScrubWindow): LocationFix[] {
  if (trail.length === 0 || (win.lo <= 0 && win.hi >= 100)) return trail;
  const t0 = new Date(trail[0]?.captured_at ?? 0).getTime();
  const t1 = new Date(trail[trail.length - 1]?.captured_at ?? 0).getTime();
  if (!(t1 > t0)) return trail;
  const start = t0 + ((t1 - t0) * win.lo) / 100;
  const end = t0 + ((t1 - t0) * win.hi) / 100;
  return trail.filter((f) => {
    const t = new Date(f.captured_at).getTime();
    return t >= start && t <= end;
  });
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
  const [windowPreset, setWindowPreset] = useState<WindowPreset>(DEFAULT_PRESET);
  // The trail's colour metric + the tapped/scrubbed point to inspect.
  const [metric, setMetric] = useState<TrailMetric>("speed");
  const [picked, setPicked] = useState<LocationFix | null>(null);
  // The dual start/stop slider (0..100) within the loaded span. It NEVER refetches —
  // it trims the in-memory trail by time — so dragging stays snappy.
  const [win, setWin] = useState<ScrubWindow>({ lo: 0, hi: 100 });
  // Heat-view tuning (the expanded History pane): per-point spot radius (px) and
  // per-point weight (how much one fix contributes to the density ramp).
  const [heatRadius, setHeatRadius] = useState(HEAT_RADIUS);
  const [heatWeight, setHeatWeight] = useState(0.4);
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
  // The viewer's own subject id — its live fixes bypass the others' coalescing.
  const selfIdRef = useRef<string | null>(null);
  selfIdRef.current = (roster ?? []).find((s) => s.is_self)?.subject_id ?? null;
  // Buffered latest live fix per OTHER subject, flushed on an interval (see below).
  const pendingRef = useRef<Map<string, LiveFix>>(new Map());
  // Coalesce map redraws to one per animation frame so slider drags stay at 60fps.
  const rafRef = useRef<number | null>(null);
  const nextStateRef = useRef<MapState | null>(null);
  const scheduleUpdate = useCallback((s: MapState) => {
    nextStateRef.current = s;
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      if (nextStateRef.current) handle.current?.update(nextStateRef.current);
    });
  }, []);

  // Create the Leaflet map once; a pin tap follows through to the switcher AND
  // inspects that person's current point (so tapping the pin shows its value even
  // when they're already the focused person — re-selecting alone is a no-op).
  useEffect(() => {
    if (!canvas.current) return;
    const h = createLocationMap(
      canvas.current,
      (id) => {
        setSel(id);
        const cur = subjectCurrentFix(rosterRef.current?.find((s) => s.subject_id === id));
        if (cur) setPicked(cur);
      },
      (fix) => setPicked(fix),
    );
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
  // focused person's trail. The server already scopes the stream to self + group. The
  // viewer's OWN device applies on every fix (snappy self-tracking); everyone else is
  // coalesced (latest-wins) to a 10 s flush so the map of others doesn't churn.
  useEffect(() => {
    const applyFix = (fix: LiveFix) => {
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
                  velocity_mps: fix.velocity_mps,
                }
              : s,
          ) ?? prev,
      );
      if (fix.subject_id === selRef.current) {
        setTrail((t) => {
          // The same GPS fix can arrive twice for the viewer's own device — once via
          // the native loopback (instant) and again when the server fans it out — so
          // skip a point identical to the tail rather than doubling the trail.
          const tail = t[t.length - 1];
          if (tail && tail.latitude === fix.lat && tail.longitude === fix.lon) return t;
          return [
            ...t,
            {
              captured_at: fix.captured_at,
              latitude: fix.lat,
              longitude: fix.lon,
              accuracy_m: fix.accuracy_m,
              battery_pct: fix.battery_pct,
              velocity_mps: fix.velocity_mps,
              // The live feed carries speed but not course/accel/altitude; the historical
              // trail (from positions) does. The live tail is just the newest few points.
              course_deg: null,
              acceleration_mps2: null,
              altitude_m: null,
            },
          ];
        });
      }
    };
    const live = connectLive((fix: LiveFix) => {
      if (fix.subject_id === selfIdRef.current) applyFix(fix);
      else pendingRef.current.set(fix.subject_id, fix); // latest wins until the flush
    });
    const flush = window.setInterval(() => {
      if (pendingRef.current.size === 0) return;
      const fixes = [...pendingRef.current.values()];
      pendingRef.current.clear();
      for (const f of fixes) applyFix(f);
    }, OTHERS_FLUSH_MS);
    // Native loopback: the Android app pushes THIS phone's own fixes straight into the
    // page (the network upload is batched up to ~30 s, so without this the self-pin
    // lags badly while driving). The payload omits subject_id — it's always this
    // viewer's own device — so stamp it with the self id and run the normal apply path.
    const w = window as Window & { __jbrainLocalFix?: ((payload: unknown) => void) | undefined };
    w.__jbrainLocalFix = (payload) => {
      const id = selfIdRef.current;
      if (!id || typeof payload !== "object" || payload === null) return;
      const p = payload as Partial<LiveFix>;
      if (typeof p.lat !== "number" || typeof p.lon !== "number") return;
      applyFix({
        subject_id: id,
        lat: p.lat,
        lon: p.lon,
        accuracy_m: p.accuracy_m ?? null,
        battery_pct: p.battery_pct ?? null,
        velocity_mps: p.velocity_mps ?? null,
        captured_at: p.captured_at ?? new Date().toISOString(),
      });
    };
    return () => {
      live.close();
      window.clearInterval(flush);
      w.__jbrainLocalFix = undefined;
    };
  }, []);

  // The focused person's trail over the time window (Everyone → no trail). On load,
  // auto-inspect their CURRENT point (the newest fix) — selecting a person brings up
  // their live detail, as if you'd tapped the right-now end of the trail.
  useEffect(() => {
    if (sel === "all") {
      setTrail([]);
      setPicked(null);
      return;
    }
    let stale = false;
    const { sinceMs, untilMs } = presetToMs(windowPreset);
    listPositions(sel, new Date(sinceMs).toISOString(), new Date(untilMs).toISOString())
      .then((fixes) => {
        if (stale) return;
        setTrail(fixes);
        const m = rosterRef.current?.find((s) => s.subject_id === sel);
        setPicked(fixes[fixes.length - 1] ?? subjectCurrentFix(m));
      })
      .catch(() => {
        if (!stale) setTrail([]);
      });
    return () => {
      stale = true;
    };
  }, [sel, windowPreset, listPositions]);

  // Keep the focused person in view: recenter (with a comfortable zoom) when first
  // selected, then smoothly PAN to follow as their live fixes move them. `roster` is a
  // dep so the follow fires on each position update; an unchanged coordinate is a no-op
  // in the map (jitter guard there).
  const focusedRef = useRef<string | null>(null);
  useEffect(() => {
    if (sel === "all") {
      focusedRef.current = null;
      return;
    }
    const p = roster?.find((s) => s.subject_id === sel);
    if (p?.latitude == null || p?.longitude == null) return;
    if (focusedRef.current === sel) {
      handle.current?.follow(p.latitude, p.longitude);
    } else {
      focusedRef.current = sel;
      handle.current?.centerOn(p.latitude, p.longitude);
    }
  }, [sel, roster]);

  // History has no meaning for Everyone — collapse it on the way there.
  useEffect(() => {
    if (sel === "all") setSheet((s) => (s === "history" ? null : s));
  }, [sel]);

  // Changing person or span resets the scrub window to full (the trail-load effect
  // owns `picked`). The deps are intentional triggers (the body only calls a setter),
  // so the exhaustive-deps heuristic doesn't apply.
  // biome-ignore lint/correctness/useExhaustiveDependencies: sel/windowPreset are reset triggers
  useEffect(() => {
    setWin({ lo: 0, hi: 100 });
  }, [sel, windowPreset]);

  // The slider trims the loaded trail in memory (no refetch → snappy).
  const visibleTrail = useMemo(() => trimByWindow(trail, win), [trail, win]);
  // "Time at place" dwell over the visible trail — only when that metric is active.
  const dwellInfo = useMemo(
    () => (metric === "timeplace" ? computeDwell(visibleTrail) : { dwell: [], max: 0 }),
    [metric, visibleTrail],
  );

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
    // The inspected point's callout: tinted + labelled in the active metric.
    const pickedIdx = picked ? visibleTrail.indexOf(picked) : -1;
    const pickedDwell = pickedIdx >= 0 ? (dwellInfo.dwell[pickedIdx] ?? 0) : 0;
    const who = roster.find((s) => s.subject_id === sel)?.label ?? "";
    const selected =
      picked && sel !== "all"
        ? {
            lat: picked.latitude,
            lon: picked.longitude,
            color: colorForFix(metric, picked, pickedDwell, dwellInfo.max),
            label: `${(who[0] ?? "•").toUpperCase()}: ${metricLabel(metric, picked, pickedDwell)}`,
          }
        : null;
    scheduleUpdate({
      mode: sel === "all" ? "live" : mode,
      fixes: sel === "all" ? [] : visibleTrail,
      metric,
      selected,
      heatRadius,
      heatWeight,
      places,
      pins,
      autoFit: sel === "all",
    });
  }, [
    roster,
    places,
    sel,
    colorOf,
    mode,
    visibleTrail,
    metric,
    picked,
    dwellInfo,
    heatRadius,
    heatWeight,
    scheduleUpdate,
  ]);

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

  // Scrubbing the slider trims in memory; if the newest fix now at the end was moving,
  // surface its readout (the "drag onto a moving point shows its info" behaviour).
  const onWin = (w: ScrubWindow) => {
    setWin(w);
    const sub = trimByWindow(trail, w);
    const newest = sub[sub.length - 1];
    setPicked(newest && (newest.velocity_mps ?? 0) >= TRAVELING_MIN_MPS ? newest : null);
  };

  // Tapping the focused person (dock) re-shows their current detail (newest fix).
  const pickCurrent = () => {
    if (sel === "all") return;
    const m = roster?.find((s) => s.subject_id === sel);
    setPicked(trail[trail.length - 1] ?? subjectCurrentFix(m));
  };

  return (
    <div className="livemap">
      <div className="livemap-canvas" ref={canvas} data-testid="map-canvas" />
      <PeopleSwitcher roster={roster} sel={sel} colorOf={colorOf} failed={failed} onPick={setSel} />
      <TileToggle scheme={tileScheme} onPick={pickScheme} />
      {sel !== "all" && (
        <MetricLegend metric={metric} dwellMax={dwellInfo.max} onPick={setMetric} />
      )}
      <DetailsSheet
        open={sheet === "details"}
        roster={roster}
        sel={sel}
        colorOf={colorOf}
        timeline={timeline}
        windowPreset={windowPreset}
        onPick={setSel}
        onClose={() => setSheet(null)}
      />
      <HistorySheet
        open={sheet === "history"}
        mode={mode}
        windowPreset={windowPreset}
        win={win}
        heatRadius={heatRadius}
        heatWeight={heatWeight}
        onMode={setMode}
        onPreset={setWindowPreset}
        onWin={onWin}
        onRadius={setHeatRadius}
        onWeight={setHeatWeight}
        onClose={() => setSheet(null)}
      />
      {picked && sel !== "all" && (
        <PointCard
          fix={picked}
          who={roster?.find((s) => s.subject_id === sel)?.label ?? "Device"}
          onClose={() => setPicked(null)}
        />
      )}
      <DockBar
        roster={roster}
        sel={sel}
        colorOf={colorOf}
        sheet={sheet}
        onToggle={toggleSheet}
        onPickCurrent={pickCurrent}
      />
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

/** A single floating icon that toggles ONLY the basemap tiles between the dark and
 * light schemes (the app's own dark chrome is untouched). It shows the scheme it will
 * switch TO, so the icon doubles as the action. */
function TileToggle({
  scheme,
  onPick,
}: {
  scheme: TileScheme;
  onPick: (s: TileScheme) => void;
}) {
  const next: TileScheme = scheme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="lm-tiles lm-float"
      aria-label={`Switch to ${next} map`}
      title={`Switch to ${next} map`}
      onClick={() => onPick(next)}
    >
      {next === "light" ? (
        // Sun (switch to the light basemap).
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      ) : (
        // Moon (switch to the dark basemap).
        <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
        </svg>
      )}
    </button>
  );
}

// --- bottom dock: a persistent bar + two pull-up sheets -------------------

function DockBar({
  roster,
  sel,
  colorOf,
  sheet,
  onToggle,
  onPickCurrent,
}: {
  roster: MemberSubject[] | null;
  sel: Selection;
  colorOf: Map<string, string>;
  sheet: Sheet;
  onToggle: (which: Exclude<Sheet, null>) => void;
  onPickCurrent: () => void;
}) {
  if (!roster) return null;
  if (roster.length === 0) {
    return <div className="lm-bar lm-float lm-bar-quiet">no one to show yet.</div>;
  }
  const everyone = sel === "all";
  const m = everyone ? null : roster.find((s) => s.subject_id === sel);
  return (
    <div className="lm-bar lm-float">
      <button
        type="button"
        className={`lm-bar-who${everyone && sheet === "details" ? " on" : ""}`}
        aria-expanded={everyone ? sheet === "details" : undefined}
        // Everyone → open the roster; a focused person → (re)show their current detail.
        onClick={() => (everyone ? onToggle("details") : onPickCurrent())}
      >
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
                {m.battery_pct !== null && (
                  <>
                    {" · "}
                    <Battery pct={m.battery_pct} />
                  </>
                )}
                {travelingSpeedMph(m.velocity_mps) && (
                  <>
                    {" · "}
                    <span className="lm-spd">{travelingSpeedMph(m.velocity_mps)}</span>
                  </>
                )}
              </span>
            </span>
          </>
        ) : null}
      </button>
      <button
        type="button"
        className={`lm-pull${!everyone && sheet === "details" ? " on" : ""}`}
        aria-expanded={!everyone && sheet === "details"}
        disabled={everyone}
        onClick={() => onToggle("details")}
      >
        <PullChevron />
        Activity
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

/** Drag-to-dismiss for a bottom sheet: a downward swipe (starting at the top of the
 * scroll, so it never fights the content scroll) follows the finger; release past a
 * threshold closes, otherwise it snaps back open. Touch-only — the grab handle's tap
 * still closes on any device. */
function useSheetDrag(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const [dragY, setDragY] = useState(0);
  const start = useRef<{ y: number; dragging: boolean } | null>(null);

  // Reset the offset whenever it closes, so a reopen starts flush.
  useEffect(() => {
    if (!open) setDragY(0);
  }, [open]);

  const onTouchStart = (e: React.TouchEvent) => {
    const el = ref.current;
    const target = e.target as Element | null;
    // Don't arm when the content is scrolled down (let it scroll) or when the touch
    // began on a slider (those own their own gesture).
    if (!el || el.scrollTop > 0 || target?.closest("input")) {
      start.current = null;
      return;
    }
    start.current = { y: e.touches[0]?.clientY ?? 0, dragging: false };
  };

  const onTouchMove = (e: React.TouchEvent) => {
    const s = start.current;
    if (!s) return;
    const dy = (e.touches[0]?.clientY ?? 0) - s.y;
    if (dy <= 0) {
      if (s.dragging) setDragY(0); // pulled back up to the top — sit flush
      return;
    }
    s.dragging = true;
    setDragY(dy);
  };

  const onTouchEnd = () => {
    const s = start.current;
    start.current = null;
    if (!s || !s.dragging) return;
    const h = ref.current?.offsetHeight ?? 400;
    // Past ~28% of the sheet (min 90px) dismisses; otherwise snap back open.
    if (dragY > Math.max(90, h * 0.28)) onClose();
    setDragY(0);
  };

  // Only override the CSS transform while actively dragging; clearing it lets the
  // sheet animate (snap back to open, or slide closed when the class drops).
  const style: React.CSSProperties | undefined =
    dragY > 0 ? { transform: `translateY(${dragY}px)`, transition: "none" } : undefined;

  return { ref, onTouchStart, onTouchMove, onTouchEnd, style };
}

function DetailsSheet({
  open,
  roster,
  sel,
  colorOf,
  timeline,
  windowPreset,
  onPick,
  onClose,
}: {
  open: boolean;
  roster: MemberSubject[] | null;
  sel: Selection;
  colorOf: Map<string, string>;
  timeline: TimelineEntry[];
  windowPreset: WindowPreset;
  onPick: (s: Selection) => void;
  onClose: () => void;
}) {
  const drag = useSheetDrag(open, onClose);
  if (!roster) return null;
  return (
    <div
      ref={drag.ref}
      className={`lm-sheet lm-float${open ? " open" : ""}`}
      aria-hidden={!open}
      style={drag.style}
      onTouchStart={drag.onTouchStart}
      onTouchMove={drag.onTouchMove}
      onTouchEnd={drag.onTouchEnd}
    >
      <button type="button" className="lm-grab" aria-label="Collapse" onClick={onClose} />
      {sel === "all" ? (
        <RosterList roster={roster} colorOf={colorOf} onPick={onPick} />
      ) : (
        <PersonActivity roster={roster} sel={sel} timeline={timeline} windowPreset={windowPreset} />
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
  windowPreset,
}: {
  roster: MemberSubject[];
  sel: Selection;
  timeline: TimelineEntry[];
  windowPreset: WindowPreset;
}) {
  const m = roster.find((s) => s.subject_id === sel);
  if (!m) return null;
  const actions = recentActions(timeline, m.subject_id, windowPreset);
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
  windowPreset,
  win,
  heatRadius,
  heatWeight,
  onMode,
  onPreset,
  onWin,
  onRadius,
  onWeight,
  onClose,
}: {
  open: boolean;
  mode: Exclude<MapMode, "live">;
  windowPreset: WindowPreset;
  win: ScrubWindow;
  heatRadius: number;
  heatWeight: number;
  onMode: (m: Exclude<MapMode, "live">) => void;
  onPreset: (p: WindowPreset) => void;
  onWin: (w: ScrubWindow) => void;
  onRadius: (r: number) => void;
  onWeight: (w: number) => void;
  onClose: () => void;
}) {
  const drag = useSheetDrag(open, onClose);
  return (
    <div
      ref={drag.ref}
      className={`lm-sheet lm-float${open ? " open" : ""}`}
      aria-hidden={!open}
      style={drag.style}
      onTouchStart={drag.onTouchStart}
      onTouchMove={drag.onTouchMove}
      onTouchEnd={drag.onTouchEnd}
    >
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
      {mode === "heat" && (
        <HeatTuning
          radius={heatRadius}
          weight={heatWeight}
          onRadius={onRadius}
          onWeight={onWeight}
        />
      )}
      <div className="lm-sec-h">Time window</div>
      <WindowPresets preset={windowPreset} onPreset={onPreset} />
      <DualSlider win={win} onWin={onWin} />
      <div className="lm-priv">Family-only · names + times only, never a coordinate.</div>
    </div>
  );
}

/** The Heat-view fine-tuning sliders (the expanded pane): per-point spot radius and
 * per-point weight, so dwell density can be read at the zoom/spread the owner wants. */
function HeatTuning({
  radius,
  weight,
  onRadius,
  onWeight,
}: {
  radius: number;
  weight: number;
  onRadius: (r: number) => void;
  onWeight: (w: number) => void;
}) {
  return (
    <div className="lm-heat">
      <label className="lm-heat-row">
        <span>Spot radius</span>
        <input
          type="range"
          min={4}
          max={50}
          step={1}
          value={radius}
          aria-label="Heat spot radius"
          onChange={(e) => onRadius(Number(e.target.value))}
        />
      </label>
      <label className="lm-heat-row">
        <span>Fix weight</span>
        <input
          type="range"
          min={0.1}
          max={1}
          step={0.05}
          value={weight}
          aria-label="Heat fix weight"
          onChange={(e) => onWeight(Number(e.target.value))}
        />
      </label>
    </div>
  );
}

/** The trail/activity time window as four "last N" presets (1h/8h/1d/7d). */
function WindowPresets({
  preset,
  onPreset,
}: {
  preset: WindowPreset;
  onPreset: (p: WindowPreset) => void;
}) {
  return (
    <div className="lm-seg lm-winpick">
      {WINDOW_PRESETS.map((p) => (
        <button
          key={p}
          type="button"
          className={preset === p ? "on" : ""}
          aria-pressed={preset === p}
          onClick={() => onPreset(p)}
        >
          {p}
        </button>
      ))}
    </div>
  );
}

/** The dual start/stop slider within the loaded span: two overlaid range inputs with
 * a filled bar for the active sub-window. Trims the in-memory trail only — no refetch
 * on drag, so it stays snappy on a long span. */
function DualSlider({ win, onWin }: { win: ScrubWindow; onWin: (w: ScrubWindow) => void }) {
  return (
    <div className="lm-range">
      <div className="lm-range-track" />
      <div className="lm-range-fill" style={{ left: `${win.lo}%`, width: `${win.hi - win.lo}%` }} />
      <input
        type="range"
        min={0}
        max={100}
        value={win.lo}
        aria-label="Window start"
        onChange={(e) => onWin({ lo: Math.min(Number(e.target.value), win.hi - 2), hi: win.hi })}
      />
      <input
        type="range"
        min={0}
        max={100}
        value={win.hi}
        aria-label="Window end"
        onChange={(e) => onWin({ lo: win.lo, hi: Math.max(Number(e.target.value), win.lo + 2) })}
      />
    </div>
  );
}

/** The legend (top-left, under the names): the active metric's colour→value scale,
 * with an icon that drops down the metric picker. */
function MetricLegend({
  metric,
  dwellMax,
  onPick,
}: {
  metric: TrailMetric;
  dwellMax: number;
  onPick: (m: TrailMetric) => void;
}) {
  const [open, setOpen] = useState(false);
  const info = legendInfo(metric, dwellMax);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [open]);
  return (
    <div className="lm-legend lm-float">
      <button
        type="button"
        className={`lm-legend-h${open ? " open" : ""}`}
        aria-haspopup="true"
        aria-label="Trail color metric"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
      >
        <svg
          className="lm-legend-ic"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 3 2 9l10 6 10-6-10-6Z" />
          <path d="M2 15l10 6 10-6" />
        </svg>
        <span className="lm-legend-nm">{info.label}</span>
        <span className="lm-legend-unit">{info.unit}</span>
      </button>
      <div className="lm-legend-ramp" style={{ background: info.gradient }} />
      <div className="lm-legend-ticks">
        {info.ticks.map((t, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed positional tick labels (may repeat, e.g. N…N)
          <span key={i}>{t}</span>
        ))}
      </div>
      {open && (
        <div className="lm-legend-menu lm-float" role="menu">
          {TRAIL_METRICS.map((m) => (
            <button
              key={m}
              type="button"
              role="menuitemradio"
              aria-checked={m === metric}
              className={m === metric ? "on" : ""}
              onClick={() => {
                onPick(m);
                setOpen(false);
              }}
            >
              <span
                className="lm-legend-sw"
                style={{ background: legendInfo(m, dwellMax).gradient }}
              />
              {METRIC_LABEL[m]}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const PC_COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

/** The tapped/scrubbed point's full readout — a compact two-column table above the
 * dock (the mock's layout). Shows everything the fix carries; "—" where unreported. */
function PointCard({
  fix,
  who,
  onClose,
}: {
  fix: LocationFix;
  who: string;
  onClose: () => void;
}) {
  const course = fix.course_deg;
  const cells: [string, string][] = [
    ["Speed", travelingSpeedMph(fix.velocity_mps) ?? "stopped"],
    [
      "Heading",
      course == null ? "—" : `${PC_COMPASS[Math.round(course / 45) % 8]} ${Math.round(course)}°`,
    ],
    ["Accel", fix.acceleration_mps2 == null ? "—" : `${fix.acceleration_mps2.toFixed(1)} m/s²`],
    ["Battery", fix.battery_pct == null ? "—" : `${fix.battery_pct}%`],
    ["Altitude", fix.altitude_m == null ? "—" : `${Math.round(fix.altitude_m)} m`],
    ["Accuracy", fix.accuracy_m == null ? "—" : `±${Math.round(fix.accuracy_m)} m`],
  ];
  return (
    <div className="lm-pointcard lm-float">
      <div className="lm-pc-h">
        <span className="lm-pc-nm">{who}</span>
        <span className="lm-pc-when">
          {lastSeen(fix.captured_at)} · {timeOfDay(fix.captured_at)}
        </span>
        <button type="button" className="lm-pc-x" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="lm-pc-grid">
        {cells.map(([k, v]) => (
          <div className="lm-pc-tr" key={k}>
            <span className="lm-pc-k">{k}</span>
            <span className="lm-pc-v">{v}</span>
          </div>
        ))}
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
function recentActions(
  timeline: TimelineEntry[],
  subjectId: string,
  windowPreset: WindowPreset,
): TimelineEntry[] {
  const { sinceMs, untilMs } = presetToMs(windowPreset);
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
