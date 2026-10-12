// The Minecraft map — a full-bleed page pushed from the Minecraft screen (binding mock
// docs/mocks/minecraft-map/a-full-bleed.html, variant A; plan §M8 and §M8a). The map fills
// the phone; one persistent bottom panel holds the world, the status line, what you tapped,
// the layers and the timeline. It is not a <Sheet>: it co-occupies the screen like the graph
// Map's panel (DESIGN "The Minecraft map").
//
// The picture is the box's 256-px biome tiles; everything over it (trails, players, deaths,
// structures, the fog, the tapped spot, labels, the scale bar) is drawn on one canvas so
// markers can cluster and labels can dodge each other and the floating controls.
//
// The timeline replays the travel log locally: one request per range, then scrubbing and
// playback only redraw. The scrubbed moment lives in a ref, so playback moves the canvas, the
// thumb and the clock every frame without re-rendering the page.

import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  type TouchEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  type MinecraftDim,
  type MinecraftExplored,
  type MinecraftMapInfo,
  type MinecraftMapPlayer,
  type MinecraftMapPlayers,
  type MinecraftMapPoint,
  type MinecraftMapStructures,
  type MinecraftSession,
  type MinecraftSlot,
  type MinecraftStructure,
  type MinecraftTimeline,
  type MinecraftTimelineEvent,
} from "../api/client";
import { useBackLayer } from "../backLayers";
import { Sheet } from "../components/Sheet";
import {
  BedIcon,
  CastleIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  ClockIcon,
  CloudOffIcon,
  CopyIcon,
  CrosshairIcon,
  EyeIcon,
  FlagIcon,
  GlobeIcon,
  GridIcon,
  InfoIcon,
  LayersIcon,
  LinkIcon,
  LocateIcon,
  LogInIcon,
  LogOutIcon,
  MapIcon,
  MinusIcon,
  PinIcon,
  PlusIcon,
  PortalIcon,
  RewindIcon,
  RouteIcon,
  SatelliteIcon,
  SkullIcon,
  UserIcon,
  XIcon,
} from "../components/icons";
import { agoOf } from "../minecraft";
import {
  type BaseLayer,
  DIMS,
  type Frame,
  GLYPHS,
  type MapView,
  PIN_SCALE,
  type Preset,
  type RosterEntry,
  type StripDay,
  TAIL_S,
  clamp,
  clusterMarkers,
  clusterTarget,
  dayRange,
  dayStrip,
  dimName,
  dimNote,
  entryLine,
  eventWords,
  fitView,
  fmtClock,
  fmtDay,
  hitTest,
  initialOf,
  nextSpeed,
  overworldSide,
  panBy,
  pathUpTo,
  playersIn,
  posAt,
  presetRange,
  rosterOf,
  scaleOfZoom,
  sessionAt,
  sessionRange,
  slotLabel,
  slotLine,
  spotText,
  statusLine,
  structureInfo,
  tileKey,
  timeStatus,
  toScreen,
  toWorld,
  tricorderLine,
  visibleTiles,
  zoomAt,
} from "../minecraftMap";
import type { MapIntent } from "../minecraftMapNav";
import { type MapSource, OWNER_MAP } from "../minecraftMapSource";
import { useForeground } from "../visibility";
import { ShareSheet } from "./MinecraftMapShare";
import { SessionsSheet, TimelinePanel, type TimelineState } from "./MinecraftMapTimeline";
import { useCovered, useW } from "./MinecraftWorlds";
import {
  LEGEND,
  type Label,
  type Marker,
  type Rect,
  type TileEntry,
  type Tokens,
  asSurvey,
  bearingLine,
  clusterBadge,
  colorOf,
  drawScale,
  drawTiles,
  fetchTile,
  grid,
  line,
  markerGlyph,
  markerLabel,
  placeLabels,
  playerDisc,
  readTokens,
  reducedMotion,
  scaleRect,
  spotMark,
  stars,
  targetMark,
  veil,
} from "./minecraftMapDraw";

/** How close a shared map opens on its player. */
const FOCUS_SCALE = 1.4;
/** Online players' positions arrive through the travel log every ~10 s. */
const PLAYERS_POLL_MS = 10_000;
/** The live view's trails and deaths: the last week, refreshed this often. */
const LIVE_LOG_S = 7 * 86_400;
const LIVE_LOG_MS = 60_000;
/** While pinned to now, the timeline's clock moves on this often. */
const NOW_TICK_MS = 5_000;
/** The panel's words follow playback this often; the canvas and the thumb every frame. */
const SHOWN_MS = 250;
/** The tile route caches a minute and the renderer re-reads the world at most once a
 *  minute, so a tile older than this is fetched again while the map is live. */
const TILE_FRESH_MS = 60_000;
const TILE_RETRY_MS = 15_000;
const TILES_IN_FLIGHT = 6;
const TILES_KEPT = 400;
const LONG_PRESS_MS = 480;
const MOVE_SLOP_PX = 6;
const TOAST_MS = 2400;
/** Below this much visible map the + / − go (pinch remains); below the second, the
 *  dimension switch and the other floating buttons go too. Back always stays. */
const SHORT_PX = 380;
const TINY_PX = 230;

type Snap = "min" | "mid" | "open";

type Sel =
  | { kind: "spot"; x: number; z: number; dim: MinecraftDim; pinned: boolean }
  /** `at` is set in time mode: where they were at that moment, not where they are. */
  | { kind: "player"; p: MinecraftMapPlayer; at?: number | undefined }
  | { kind: "struct"; s: MinecraftStructure; dim: MinecraftDim }
  | { kind: "event"; e: MinecraftTimelineEvent }
  | { kind: "spawn"; x: number; z: number };

type Hit =
  | { kind: "player"; sx: number; sy: number; p: MinecraftMapPlayer }
  | { kind: "marker"; sx: number; sy: number; m: Marker }
  | { kind: "cluster"; sx: number; sy: number; members: Marker[] };

const TL_OFF: TimelineState = {
  on: false,
  live: true,
  from: 0,
  to: 0,
  playing: false,
  speed: 60,
  follow: false,
  focus: null,
  cur: null,
  preset: "session",
  hidden: [],
};

const nowS = () => Date.now() / 1000;

/** Whether a session in this range is still open, so its player holds their last spot. */
const openIn = (log: MinecraftTimeline | null) => (id: string) =>
  !!log?.sessions?.some((x) => x.id === id && x.end === null);

interface Overlays {
  players: boolean;
  trails: boolean;
  deaths: boolean;
  spawn: boolean;
  structures: boolean;
  explored: boolean;
  grid: boolean;
}

const OVERLAYS: Overlays = {
  players: true,
  trails: true,
  deaths: true,
  spawn: true,
  structures: true,
  explored: false,
  grid: false,
};

// The base layers the box renders, each with its swatch: a built-on plank floor for
// true colour, a forest for the biome atlas. Height is listed, not built yet.
const BASE_LAYERS: [BaseLayer, string, string, [number, number, number]][] = [
  ["surface", "Surface", "true colour where explored", [162, 130, 78]],
  ["biomes", "Biomes", "explored + satellite survey", [5, 102, 33]],
];
const LAYER_KEY = "jbrain.minecraftMap.layer";

/** The base layer this viewer last picked: a per-device convenience, Surface by default. */
function storedLayer(): BaseLayer {
  try {
    return localStorage.getItem(LAYER_KEY) === "biomes" ? "biomes" : "surface";
  } catch {
    return "surface";
  }
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Can't reach the box.";
}

/** An unreachable box (or sidecar) rather than a refusal about this world. */
const isUnreachable = (err: unknown) =>
  !(err instanceof ApiError) || err.status >= 502 || err.status === 0;

/** What the map page needs from around it: the owner's worlds, or one shared world. */
export interface MapHost {
  source: MapSource;
  slots: MinecraftSlot[];
  slotOf: (id: string) => MinecraftSlot | null;
  startSlot: string;
  running: boolean;
  onlineCount: number;
  nowMs: number;
  covered: boolean;
  root: RefObject<HTMLDivElement>;
  /** The owner's way back and way to share; a shared view has neither, nor a world picker. */
  owner: { pop: () => void } | null;
  /** A shared view opens on this player (an xuid key) instead of fitting everyone. */
  focus?: string | undefined;
}

/** The owner's map page. `at` opens it on one spot (a chat card's view) instead of fitting
 *  everyone. */
export function MapLayer({
  slot: startSlot,
  at,
}: {
  slot?: string | undefined;
  at?: MapIntent | undefined;
}) {
  const w = useW();
  useBackLayer(w.pop);
  const root = useRef<HTMLDivElement>(null);
  const covered = useCovered(w, root);
  return (
    <MapPage
      at={at}
      host={{
        source: OWNER_MAP,
        slots: w.slots,
        slotOf: w.slotOf,
        startSlot: startSlot ?? w.active?.id ?? "slot1",
        running: w.running,
        onlineCount: w.online.length,
        nowMs: w.nowMs,
        covered,
        root,
        owner: { pop: w.pop },
      }}
    />
  );
}

/** The map itself, for whichever host it sits in. */
export function MapPage({ host, at }: { host: MapHost; at?: MapIntent | undefined }) {
  const { source, owner, covered, root } = host;
  const pop = owner?.pop;
  const back = useRef<HTMLButtonElement>(null);
  const mapEl = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const dragEl = useRef<HTMLDivElement>(null);
  const selEl = useRef<HTMLDivElement>(null);
  const gridEl = useRef<HTMLDivElement>(null);
  const foreground = useForeground();

  const [slot, setSlot] = useState(host.startSlot);
  const [dim, setDim] = useState<MinecraftDim>(at?.dim ?? "overworld");
  const [snap, setSnap] = useState<Snap>("min");
  const [sel, setSel] = useState<Sel | null>(() =>
    at?.mark
      ? { kind: "spot", x: Math.floor(at.x), z: Math.floor(at.z), dim: at.dim, pinned: false }
      : null,
  );
  const [point, setPoint] = useState<MinecraftMapPoint | null>(null);
  const [ov, setOv] = useState<Overlays>(OVERLAYS);
  const [layer, setLayerState] = useState<BaseLayer>(storedLayer);
  const setLayer = useCallback((l: BaseLayer) => {
    setLayerState(l);
    try {
      localStorage.setItem(LAYER_KEY, l);
    } catch {
      // A private window keeps the choice for this visit only.
    }
  }, []);
  /** Explored by: null is anyone, else one player's key. */
  const [exBy, setExBy] = useState<string | null>(null);
  const [tab, setTab] = useState<"layers" | "time">("layers");
  const [tl, setTl] = useState<TimelineState>(TL_OFF);
  /** The scrubbed moment as the panel last showed it; the ref below moves every frame. */
  const [tShown, setTShown] = useState(nowS);
  const [rangeLog, setRangeLog] = useState<MinecraftTimeline | null>(null);
  const [liveLog, setLiveLog] = useState<MinecraftTimeline | null>(null);
  const [fog, setFog] = useState<MinecraftExplored | null>(null);
  const [list, setList] = useState<{ who: string | null } | null>(null);
  const [trackW, setTrackW] = useState(300);
  const [info, setInfo] = useState<MinecraftMapInfo | null>(null);
  const [players, setPlayers] = useState<MinecraftMapPlayers | null>(null);
  const [playersFailed, setPlayersFailed] = useState(false);
  const [structs, setStructs] = useState<MinecraftMapStructures | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [tiles, setTiles] = useState({ visible: 0, pending: 0 });
  const [picker, setPicker] = useState(false);
  const [panelH, setPanelH] = useState(160);
  const [vis, setVis] = useState(600);
  const [toast, setToast] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);

  const slotRec: MinecraftSlot | null = host.slotOf(slot);
  const loaded = !!slotRec?.active;
  const live = loaded && (players?.live ?? host.running);
  const onlineCount = players ? players.players.filter((p) => p.online).length : host.onlineCount;
  // The time-mode data is the range's; the live view's overlays are the last week's.
  const log = tl.on ? rangeLog : liveLog;
  const sessions = rangeLog?.sessions ?? liveLog?.sessions ?? [];
  const roster = rosterOf([
    ...(players?.players ?? []),
    ...sessions.map((x) => ({ xuid: x.xuid, gamertag: x.gamertag })),
  ]);
  const timeTravel = tl.on && !tl.live;
  const aimer =
    sel?.kind === "player" && !timeTravel
      ? players?.players.find((q) => q.xuid === sel.p.xuid)
      : undefined;
  const tricorder = aimer?.target ? tricorderLine(aimer, aimer.target) : null;

  // Everything the draw loop reads, current as of the last render.
  const view = useRef<MapView>(
    at ? { x: at.x + 0.5, z: at.z + 0.5, s: scaleOfZoom(at.zoom) } : { x: 0, z: 0, s: 0.7 },
  );
  const frame = useRef<Frame>({ w: 1, h: 1, bottom: 160, top: 64 });
  const dirty = useRef(true);
  const hits = useRef<Hit[]>([]);
  const cache = useRef(new Map<string, TileEntry>());
  const inFlight = useRef(0);
  // Opened on a spot, the first view is that spot; changing world or dimension fits again.
  const needsFit = useRef(!at);
  const tk = useRef<Tokens | null>(null);
  const live$ = useRef(live);
  live$.current = live;
  const tRef = useRef(tShown);
  const shownAt = useRef(0);
  /** The followed player's dimension a moment ago, to see them change it. */
  const focusDim = useRef<{ dim: MinecraftDim; t: number } | null>(null);
  const thumbEl = useRef<HTMLDivElement>(null);
  const fillEl = useRef<HTMLDivElement>(null);
  const clockEl = useRef<HTMLElement>(null);
  const dayEl = useRef<HTMLSpanElement>(null);
  const trackEl = useRef<HTMLDivElement>(null);
  const scrubEl = useRef<HTMLDivElement>(null);
  const state = useRef({
    slot,
    dim,
    layer,
    sel,
    ov,
    players,
    structs,
    info,
    tl,
    log,
    roster,
    fog,
    exBy,
  });
  state.current = {
    slot,
    dim,
    layer,
    sel,
    ov,
    players,
    structs,
    info,
    tl,
    log,
    roster,
    fog,
    exBy,
  };

  const redraw = useCallback(() => {
    dirty.current = true;
  }, []);
  // biome-ignore lint/correctness/useExhaustiveDependencies: any change to what's drawn redraws.
  useEffect(redraw, [slot, dim, layer, sel, ov, players, structs, info, panelH, tl, log, fog]);

  // A pushed page takes focus at its back button.
  useEffect(() => {
    back.current?.focus({ preventScroll: true });
  }, []);

  const say = useCallback((msg: string) => setToast(msg), []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), TOAST_MS);
    return () => clearTimeout(t);
  }, [toast]);

  // ---- data ----

  useEffect(() => {
    let gone = false;
    setInfo(null);
    source
      .info(slot, dim)
      .then((i) => {
        if (gone) return;
        setInfo(i);
        setUnreachable(false);
        setProblem(null);
      })
      .catch((err) => {
        if (gone) return;
        if (isUnreachable(err)) setUnreachable(true);
        else setProblem(errText(err));
      });
    return () => {
      gone = true;
    };
  }, [source, slot, dim]);

  useEffect(() => {
    if (!foreground) return;
    let gone = false;
    const read = () =>
      source
        .players(slot)
        .then((p) => {
          if (gone) return;
          setPlayers(p);
          setPlayersFailed(false);
          setUnreachable(false);
        })
        .catch((err) => {
          if (gone) return;
          setPlayersFailed(true);
          if (isUnreachable(err)) setUnreachable(true);
        });
    void read();
    const t = setInterval(read, PLAYERS_POLL_MS);
    return () => {
      gone = true;
      clearInterval(t);
    };
  }, [source, slot, foreground]);

  const sweeping = structs?.sweep.state === "running";
  useEffect(() => {
    let gone = false;
    const read = () =>
      source
        .structures(slot, dim)
        .then((s) => {
          if (!gone) setStructs(s);
        })
        .catch(() => {
          if (!gone) setStructs(null);
        });
    void read();
    if (!sweeping || !foreground) {
      return () => {
        gone = true;
      };
    }
    const t = setInterval(read, PLAYERS_POLL_MS);
    return () => {
      gone = true;
      clearInterval(t);
    };
  }, [source, slot, dim, sweeping, foreground]);

  // What a tapped spot or structure stands on.
  useEffect(() => {
    if (!sel || (sel.kind !== "spot" && sel.kind !== "struct")) {
      setPoint(null);
      return;
    }
    const [x, z] = sel.kind === "spot" ? [sel.x, sel.z] : [sel.s.x, sel.s.z];
    let gone = false;
    setPoint(null);
    source
      .point(slot, sel.dim, x, z)
      .then((p) => {
        if (!gone) setPoint(p);
      })
      .catch(() => {
        if (!gone) setPoint({ x, z, dimension: sel.dim, source: "unknown" });
      });
    return () => {
      gone = true;
    };
  }, [source, sel, slot]);

  // The live view's trails and deaths: the last week of the travel log.
  useEffect(() => {
    if (!foreground) return;
    let gone = false;
    const read = () => {
      const now = nowS();
      source
        .timeline(slot, now - LIVE_LOG_S, now)
        .then((got) => {
          if (!gone) setLiveLog(got);
        })
        .catch(() => {
          if (!gone) setLiveLog(null);
        });
    };
    read();
    if (!live) {
      return () => {
        gone = true;
      };
    }
    const t = setInterval(read, LIVE_LOG_MS);
    return () => {
      gone = true;
      clearInterval(t);
    };
  }, [source, slot, live, foreground]);

  // The timeline's range: one request, then everything inside it is a local redraw. A
  // range pinned to now grows with the clock without asking again.
  useEffect(() => {
    if (!tl.on) {
      setRangeLog(null);
      return;
    }
    let gone = false;
    source
      .timeline(slot, tl.from, tl.to)
      .then((got) => {
        if (!gone) setRangeLog(got);
      })
      .catch((err) => {
        if (gone) return;
        if (isUnreachable(err)) setUnreachable(true);
        else say(errText(err));
      });
    return () => {
      gone = true;
    };
  }, [source, slot, tl.on, tl.from, tl.to, say]);

  // Explored by: the fog for this dimension, lifted by first-seen time on the timeline.
  useEffect(() => {
    if (!ov.explored) {
      setFog(null);
      return;
    }
    let gone = false;
    const now = nowS();
    source
      .timeline(slot, now - 86_400, now, {
        include: ["explored"],
        dim,
        players: exBy ? [exBy] : [],
      })
      .then((got) => {
        if (!gone) setFog(got.explored ?? null);
      })
      .catch(() => {
        if (!gone) setFog(null);
      });
    return () => {
      gone = true;
    };
  }, [source, slot, dim, ov.explored, exBy]);

  // ---- view ----

  const glide = useCallback(
    (to: MapView) => {
      const from = { ...view.current };
      const t0 = performance.now();
      if (reducedMotion()) {
        view.current = to;
        redraw();
        return;
      }
      const step = (now: number) => {
        // A frame's timestamp is when the frame began, which can be before the glide did:
        // a negative step would zoom the wrong way, exponentially.
        const f = clamp((now - t0) / 200, 0, 1);
        const e = 1 - (1 - f) ** 3;
        view.current = {
          x: from.x + (to.x - from.x) * e,
          z: from.z + (to.z - from.z) * e,
          s: from.s * (to.s / from.s) ** e,
        };
        redraw();
        if (f < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    [redraw],
  );

  /** Everyone online in this dimension, else anyone; else the explored ground; else spawn. */
  const fitEveryone = useCallback(
    (animate: boolean) => {
      const st = state.current;
      const here = playersIn(st.players?.players ?? [], st.dim);
      const on = here.filter((p) => p.online);
      let pts: [number, number][] = (on.length ? on : here).map((p) => [p.x, p.z]);
      // On the replay, everyone is where they were at the moment, not where they are.
      if (st.tl.on && !st.tl.live) {
        pts = [];
        for (const tr of st.log?.trails ?? []) {
          if (st.tl.hidden.includes(tr.xuid)) continue;
          const pos = posAt(tr, tRef.current, openIn(st.log));
          if (pos && pos.dim === st.dim) pts.push([pos.x, pos.z]);
        }
      }
      const ext = st.info?.extent;
      if (!pts.length && ext) {
        pts.push([ext.min_x, ext.min_z], [ext.max_x, ext.max_z]);
      }
      // Nobody has been here: the End opens wide enough to show its outer islands.
      const to = fitView(pts, frame.current) ?? {
        x: 0,
        z: 0,
        s: st.dim === "the_end" ? 0.15 : 0.5,
      };
      if (animate) glide(to);
      else {
        view.current = to;
        redraw();
      }
    },
    [glide, redraw],
  );

  // Fit once per world and dimension, when what to fit has arrived. A shared map opens on
  // its player instead, in whichever dimension they're in, the first time it can.
  const focusOn = useRef(host.focus ?? null);
  useEffect(() => {
    if (!needsFit.current || !info || (!players && !playersFailed)) return;
    const p = players?.players.find((q) => q.xuid === focusOn.current);
    focusOn.current = null;
    if (p && p.dim !== state.current.dim) {
      setDim(p.dim);
      focusOn.current = p.xuid;
      return;
    }
    needsFit.current = false;
    if (!p) {
      fitEveryone(false);
      return;
    }
    view.current = { x: p.x + 0.5, z: p.z + 0.5, s: FOCUS_SCALE };
    redraw();
    setSel({ kind: "player", p });
  }, [info, players, playersFailed, fitEveryone, redraw]);

  const select = useCallback((next: Sel | null) => {
    setSel(next);
    setSnap((s) => (s === "open" ? "mid" : s));
  }, []);

  function changeDim(d: MinecraftDim) {
    if (d === dim) return;
    setDim(d);
    setSel(null);
    needsFit.current = true;
  }

  function changeSlot(id: string) {
    setPicker(false);
    if (id === slot) return;
    setSlot(id);
    setDim("overworld");
    setSel(null);
    setPlayers(null);
    setStructs(null);
    setLiveLog(null);
    setTl(TL_OFF);
    setTab("layers");
    needsFit.current = true;
  }

  // ---- the timeline ----

  /** The replay crossed into another dimension: show that one, keeping the view on them. */
  const jumpDim = useCallback((d: MinecraftDim) => {
    state.current.dim = d;
    setDim(d);
    setSel(null);
  }, []);

  /** Follow, and the jump into another dimension when the followed player takes one. */
  const followAt = useCallback(
    (t: number) => {
      const st = state.current;
      const tls = st.tl;
      const trails = st.log?.trails;
      if (!tls.on || !tls.focus || !trails) return;
      const trail = trails.find((x) => x.xuid === tls.focus);
      const pos = trail ? posAt(trail, t, openIn(st.log)) : null;
      if (!trail || !pos) return;
      const before = focusDim.current;
      focusDim.current = { dim: pos.dim, t };
      if ((tls.follow || tls.playing) && pos.dim !== st.dim) {
        if (before && before.dim !== pos.dim) {
          // A respawn crossed on the way here is the jump from the death to the bed.
          const respawned = (st.log?.events ?? []).some(
            (e) =>
              e.kind === "respawn" &&
              e.xuid === trail.xuid &&
              e.at > Math.min(before.t, t) &&
              e.at <= Math.max(before.t, t),
          );
          jumpDim(pos.dim);
          say(dimNote(trail.gamertag, pos.dim, respawned));
        } else if (tls.follow) {
          jumpDim(pos.dim);
        }
      }
      if (tls.follow && pos.dim === state.current.dim) {
        view.current = { ...view.current, x: pos.x, z: pos.z };
      }
    },
    [jumpDim, say],
  );

  /** Move the scrubbed moment: the canvas, the thumb and the clock now, the panel's words a
   *  few times a second. `quiet` skips Follow, for jumps that place the view themselves. */
  const moveTo = useCallback(
    (t: number, opts: { quiet?: boolean; force?: boolean } = {}) => {
      tRef.current = t;
      const tls = state.current.tl;
      const pc = clamp(((t - tls.from) / Math.max(1, tls.to - tls.from)) * 100, 0, 100);
      if (thumbEl.current) thumbEl.current.style.left = `${pc}%`;
      if (fillEl.current) fillEl.current.style.width = `${pc}%`;
      if (clockEl.current) clockEl.current.textContent = fmtClock(t);
      const ms = performance.now();
      if (opts.force || ms - shownAt.current > SHOWN_MS) {
        shownAt.current = ms;
        setTShown(t);
      }
      if (!opts.quiet) followAt(t);
      redraw();
    },
    [followAt, redraw],
  );

  /** Centre on a world point in its dimension, switching to it if need be. */
  function showPlace(d: MinecraftDim, x: number, z: number, minS = 0.9) {
    if (d !== state.current.dim) jumpDim(d);
    glide({ x: x + 0.5, z: z + 0.5, s: Math.max(view.current.s, minS) });
  }

  function openTimeline() {
    setTab("time");
    setSnap("mid");
    if (tl.on) return;
    const now = nowS();
    const [from, to] = presetRange("session", sessions, now);
    const on = (players?.players ?? []).find((p) => p.online);
    setTl({ ...TL_OFF, on: true, live: true, from, to, focus: on?.xuid ?? null });
    focusDim.current = null;
    moveTo(now, { quiet: true, force: true });
  }

  function loadSession(x: MinecraftSession) {
    const now = nowS();
    const [from, to] = sessionRange(x, now);
    setTl((c) => ({
      ...c,
      on: true,
      live: false,
      from,
      to,
      cur: x.id,
      focus: x.xuid,
      preset: null,
      playing: false,
      hidden: c.hidden.filter((h) => h !== x.xuid),
    }));
    setSel(null);
    setList(null);
    setTab("time");
    setSnap("mid");
    focusDim.current = x.first ? { dim: x.first.dim, t: x.start } : null;
    if (x.first) showPlace(x.first.dim, x.first.x, x.first.z);
    moveTo(x.start, { quiet: true, force: true });
  }

  function showDay(d: StripDay) {
    const now = nowS();
    const r = dayRange(d.sessions, now);
    const first = d.sessions[0];
    if (!r || !first) return;
    setTl((c) => ({
      ...c,
      on: true,
      live: false,
      from: r[0],
      to: r[1],
      cur: null,
      preset: null,
      playing: false,
      focus: first.xuid,
    }));
    setSel(null);
    focusDim.current = first.first ? { dim: first.first.dim, t: first.start } : null;
    if (first.first) showPlace(first.first.dim, first.first.x, first.first.z);
    moveTo(first.start, { quiet: true, force: true });
  }

  function setPreset(p: Preset) {
    const now = nowS();
    const [from, to] = presetRange(p, sessions, now);
    const t = tl.live ? now : clamp(tRef.current, from, to);
    setTl((c) => ({ ...c, on: true, from, to, preset: p, cur: null, playing: false }));
    moveTo(t, { quiet: true, force: true });
  }

  function goNow() {
    const now = nowS();
    const [from, to] = presetRange("session", sessions, now);
    setTl((c) => ({ ...c, live: true, playing: false, from, to, preset: "session", cur: null }));
    moveTo(now, { quiet: true, force: true });
  }

  function backToLive() {
    setTl((c) => ({ ...TL_OFF, speed: c.speed }));
    setTab("layers");
    if (sel?.kind === "event") setSel(null);
    moveTo(nowS(), { quiet: true, force: true });
  }

  function togglePlay() {
    if (tl.playing) {
      setTl((c) => ({ ...c, playing: false }));
      setTShown(tRef.current);
      return;
    }
    // From the live edge, or the end of the range, a replay starts at the range's start.
    if (tl.live || tRef.current >= tl.to - 0.5) moveTo(tl.from, { quiet: true, force: true });
    setTl((c) => ({ ...c, live: false, playing: true }));
  }

  function toggleFollow() {
    const on = !tl.follow;
    setTl((c) => ({ ...c, follow: on }));
    if (!on || !tl.focus) return;
    const trail = log?.trails?.find((x) => x.xuid === tl.focus);
    const pos = trail ? posAt(trail, tRef.current, openIn(log)) : null;
    if (pos) showPlace(pos.dim, pos.x, pos.z, view.current.s);
  }

  function scrubTo(t: number) {
    if (tl.live || tl.playing) setTl((c) => ({ ...c, live: false, playing: false }));
    moveTo(t, { force: true });
  }

  function jumpToEvent(e: MinecraftTimelineEvent) {
    if (tl.live || tl.playing) setTl((c) => ({ ...c, live: false, playing: false }));
    moveTo(e.at, { force: true });
    if (e.dim && e.x !== null && e.z !== null) showPlace(e.dim, e.x, e.z, 1.4);
    if (e.kind === "death") setSel({ kind: "event", e });
  }

  function followPlayer(xuid: string) {
    setSel(null);
    if (!tl.on) {
      const now = nowS();
      const [from, to] = presetRange("session", sessions, now);
      setTl({ ...TL_OFF, on: true, live: true, from, to, focus: xuid, follow: true });
      moveTo(now, { quiet: true, force: true });
    } else {
      setTl((c) => ({
        ...c,
        focus: xuid,
        follow: true,
        hidden: c.hidden.filter((h) => h !== xuid),
      }));
    }
    focusDim.current = null;
    setTab("time");
    setSnap("mid");
    const p = players?.players.find((q) => q.xuid === xuid);
    if (p && !tl.on) showPlace(p.dim, p.x, p.z, view.current.s);
  }

  function replayBefore(e: MinecraftTimelineEvent) {
    const now = nowS();
    const x = sessionAt(sessions, e.xuid, e.at, now);
    if (!x) return;
    const [from, to] = sessionRange(x, now);
    const t = Math.max(x.start, e.at - 180);
    setTl((c) => ({
      ...c,
      on: true,
      live: false,
      from,
      to,
      cur: x.id,
      focus: e.xuid,
      preset: null,
      playing: true,
      speed: 10,
      follow: true,
      hidden: c.hidden.filter((h) => h !== e.xuid),
    }));
    setSel(null);
    setTab("time");
    setSnap("mid");
    focusDim.current = null;
    if (e.dim && e.x !== null && e.z !== null) showPlace(e.dim, e.x, e.z, 1.4);
    moveTo(t, { quiet: true, force: true });
  }

  // Pinned to now, the scrubbed moment follows the clock.
  useEffect(() => {
    if (!tl.on || !tl.live) return;
    const tick = () => moveTo(nowS(), { force: true });
    tick();
    const id = setInterval(tick, NOW_TICK_MS);
    return () => clearInterval(id);
  }, [tl.on, tl.live, moveTo]);

  // Playback: a local redraw every frame, never a request.
  useEffect(() => {
    if (!tl.on || !tl.playing) return;
    let raf = 0;
    let last = performance.now();
    const step = (ts: number) => {
      const dt = clamp((ts - last) / 1000, 0, 0.1);
      last = ts;
      const st = state.current.tl;
      const t = Math.min(st.to, tRef.current + dt * st.speed);
      moveTo(t);
      if (t >= st.to) {
        setTl((c) => ({ ...c, playing: false }));
        setTShown(t);
        return;
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [tl.on, tl.playing, moveTo]);

  // The scrubber's width, so its glyphs merge below the 44 px tap floor.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the track mounts with the Timeline tab.
  useEffect(() => {
    const el = trackEl.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setTrackW(el.getBoundingClientRect().width || 300));
    ro.observe(el);
    return () => ro.disconnect();
  }, [tab]);

  // ---- canvas: size, tiles, drawing ----

  useEffect(() => {
    const el = mapEl.current;
    if (!el) return;
    const fit = () => {
      const r = el.getBoundingClientRect();
      frame.current = { ...frame.current, w: r.width, h: r.height };
      const c = canvas.current;
      if (c) {
        const d = window.devicePixelRatio || 1;
        c.width = Math.max(1, Math.round(r.width * d));
        c.height = Math.max(1, Math.round(r.height * d));
      }
      redraw();
    };
    fit();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, [redraw]);

  // The theme can change under the page; re-read the tokens when it does.
  useEffect(() => {
    const mo =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(() => {
            tk.current = null;
            redraw();
          });
    mo?.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => mo?.disconnect();
  }, [redraw]);

  const want = useCallback(
    (
      key: string,
      t: { zoom: number; tx: number; tz: number },
      layer: BaseLayer,
    ): TileEntry | null => {
      const st = state.current;
      const now = Date.now();
      const have = cache.current.get(key);
      const stale =
        have &&
        ((have.state === "ok" && live$.current && now - have.at > TILE_FRESH_MS) ||
          (have.state === "failed" && now - have.at > TILE_RETRY_MS));
      if (have && !stale) return have;
      if (have?.state === "loading" || inFlight.current >= TILES_IN_FLIGHT) return have ?? null;
      const entry: TileEntry = { img: have?.img ?? null, at: now, state: "loading" };
      cache.current.set(key, entry);
      inFlight.current++;
      fetchTile(st.slot, st.dim, t.zoom, t.tx, t.tz, layer, source.tileUrl)
        .then((img) => {
          cache.current.set(key, { img, at: Date.now(), state: "ok" });
        })
        .catch(() => {
          cache.current.set(key, { img: entry.img, at: Date.now(), state: "failed" });
        })
        .finally(() => {
          inFlight.current--;
          if (cache.current.size > TILES_KEPT) {
            const first = cache.current.keys().next().value;
            if (first !== undefined) cache.current.delete(first);
          }
          redraw();
        });
      return entry;
    },
    [source.tileUrl, redraw],
  );

  const draw = useCallback(() => {
    const c = canvas.current;
    const ctx = c?.getContext?.("2d");
    const el = mapEl.current;
    if (!c || !ctx || !el) return;
    const st = state.current;
    if (!tk.current) tk.current = readTokens(el);
    const t = tk.current;
    const f = frame.current;
    const v = view.current;
    const d = c.width / Math.max(1, f.w);
    ctx.setTransform(d, 0, 0, d, 0, 0);
    ctx.fillStyle = t.voidc;
    ctx.fillRect(0, 0, f.w, f.h);
    if (st.dim === "the_end") stars(ctx, f, t);

    // tiles
    ctx.imageSmoothingEnabled = false;
    const spots = visibleTiles(v, f);
    const pending = drawTiles(
      ctx,
      spots,
      st.layer,
      (s, layer) => cache.current.get(tileKey(st.slot, st.dim, s, layer)),
      (s, layer) => want(tileKey(st.slot, st.dim, s, layer), s, layer),
      t,
    );
    setTiles((p) =>
      p.visible === spots.length && p.pending === pending ? p : { visible: spots.length, pending },
    );

    const labels: Label[] = [];
    const nextHits: Hit[] = [];
    const tls = st.tl;
    const lg = st.log;
    const at = tRef.current;
    // Pinned to now, the timeline draws like the live view; anywhere else it's the replay.
    const replay = tls.on && !tls.live;
    const shown = (x: string) => !tls.on || !tls.hidden.includes(x);
    const tokenOf = (x: string) => st.roster.find((r) => r.xuid === x)?.token ?? "--steel";
    const off = (sx: number, sy: number) => sx < -30 || sy < -30 || sx > f.w + 30 || sy > f.h + 30;

    if (st.ov.explored && st.fog && st.fog.dim === st.dim) {
      veil(ctx, v, f, t, st.fog.chunks, tls.on ? at : Number.POSITIVE_INFINITY);
    }
    if (st.ov.grid) grid(ctx, v, f, t, labels);

    // trails: on the timeline everyone's path up to the moment, faint, with a fading tail;
    // live, each player's latest session
    if (st.ov.trails && lg?.trails) {
      for (const tr of lg.trails) {
        if (!shown(tr.xuid)) continue;
        const col = colorOf(t, tokenOf(tr.xuid));
        const latest = tr.segments[tr.segments.length - 1];
        for (const seg of tr.segments) {
          if (seg.dim !== st.dim) continue;
          if (!tls.on) {
            if (!latest || (latest.session ? seg.session !== latest.session : seg !== latest))
              continue;
            line(ctx, v, f, pathUpTo(seg, Number.POSITIVE_INFINITY), col, t, "full");
            continue;
          }
          const first = seg.points[0];
          const last = seg.points[seg.points.length - 1];
          if (!first || !last || first[0] > at || last[0] < tls.from) continue;
          line(
            ctx,
            v,
            f,
            pathUpTo(seg, at, Number.NEGATIVE_INFINITY, tr.downsampled),
            col,
            t,
            "faint",
          );
          line(ctx, v, f, pathUpTo(seg, at, at - TAIL_S, tr.downsampled), col, t, "tail");
        }
      }
    }

    // point markers — structures, deaths, the world spawn — clustered together
    const sel = st.sel;
    const isSel = (m: Marker) =>
      (m.type === "struct" &&
        sel?.kind === "struct" &&
        sel.s.x === m.s.x &&
        sel.s.z === m.s.z &&
        sel.s.structure === m.s.structure) ||
      (m.type === "event" && sel?.kind === "event" && sel.e === m.e) ||
      (m.type === "spawn" && sel?.kind === "spawn");
    const markers: Marker[] = [];
    const mark = (m: Marker) => {
      if (!off(m.sx, m.sy)) markers.push(m);
    };
    if (st.ov.structures && st.structs && st.structs.dimension === st.dim) {
      for (const s of st.structs.structures) {
        const [sx, sy] = toScreen(v, f, s.x + 0.5, s.z + 0.5);
        mark({ type: "struct", s, x: s.x, z: s.z, sx, sy, pr: 5 });
      }
    }
    const spawn = st.info?.spawn;
    if (st.ov.spawn && spawn && st.dim === "overworld") {
      const [sx, sy] = toScreen(v, f, spawn.x + 0.5, spawn.z + 0.5);
      mark({ type: "spawn", x: spawn.x, z: spawn.z, sx, sy, pr: 4 });
    }
    // Deaths, live and on the timeline; on the replay also where people logged in and off
    // and went through a portal, each up to the moment.
    if (lg?.events) {
      for (const e of lg.events) {
        if (e.dim !== st.dim || e.x === null || e.z === null || !shown(e.xuid)) continue;
        if (tls.on && (e.at > at || e.at < tls.from)) continue;
        const death = e.kind === "death";
        if (death ? !st.ov.deaths : !replay || e.kind === "respawn") continue;
        const [sx, sy] = toScreen(v, f, e.x + 0.5, e.z + 0.5);
        mark({ type: "event", e, x: e.x, z: e.z, sx, sy, pr: death ? 1 : 3 });
      }
    }
    const tapped = markers.filter(isSel);
    for (const cl of clusterMarkers(markers.filter((m) => !isSel(m)))) {
      const one = cl.members[0];
      if (cl.members.length === 1 && one) {
        markerGlyph(ctx, one, t, false);
        const txt = markerLabel(one);
        if (txt && (v.s >= 0.25 || one.type === "spawn"))
          labels.push({ txt, x: one.sx, y: one.sy - 8, pr: one.pr });
        nextHits.push({ kind: "marker", sx: one.sx, sy: one.sy, m: one });
      } else {
        clusterBadge(ctx, cl, t);
        nextHits.push({ kind: "cluster", sx: cl.sx, sy: cl.sy, members: cl.members });
      }
    }

    // The selected player's Tricorder target, with a dashed bearing from where they stand
    // (live view only: the target is where Dave has it pointed now, not then).
    const aimed = sel?.kind === "player" && !replay ? sel.p.xuid : null;
    const aimer = aimed ? st.players?.players.find((q) => q.xuid === aimed) : undefined;
    const aim = aimer?.target;
    if (aimer && aim && aim.dim === st.dim) {
      const col = colorOf(t, tokenOf(aimer.xuid));
      const tx = aim.x + 0.5;
      const tz = aim.z + 0.5;
      if (aimer.dim === st.dim) bearingLine(ctx, v, f, [aimer.x, aimer.z], [tx, tz], col, t);
      const [sx, sy] = toScreen(v, f, tx, tz);
      targetMark(ctx, sx, sy, col, t);
      labels.push({ txt: aim.label, x: sx, y: sy - 14, pr: -1 });
    }

    // players: never clustered; online ones pulse. On the replay, each shown player is
    // where the travel log puts them at the moment, interpolated between samples.
    let animate = false;
    const pinnedOn = sel?.kind === "player" ? sel.p.xuid : null;
    if (replay && lg?.trails) {
      for (const tr of lg.trails) {
        if (!shown(tr.xuid)) continue;
        const pos = posAt(tr, at, openIn(lg));
        if (!pos || pos.dim !== st.dim) continue;
        const [sx, sy] = toScreen(v, f, pos.x, pos.z);
        if (off(sx, sy)) continue;
        const p: MinecraftMapPlayer = {
          xuid: tr.xuid,
          gamertag: tr.gamertag,
          dim: pos.dim,
          x: pos.x,
          y: pos.y,
          z: pos.z,
          at,
          online: true,
        };
        const on = pinnedOn === tr.xuid;
        playerDisc(ctx, sx, sy, p, colorOf(t, tokenOf(tr.xuid)), t, on, false);
        labels.push({ txt: tr.gamertag, x: sx, y: sy - 8, pr: on ? -1 : 0 });
        nextHits.push({ kind: "player", sx, sy, p });
      }
    } else if ((st.ov.players || tls.on) && st.players) {
      for (const p of playersIn(st.players.players, st.dim)) {
        if (!shown(p.xuid)) continue;
        const [sx, sy] = toScreen(v, f, p.x, p.z);
        if (off(sx, sy)) continue;
        const on = pinnedOn === p.xuid;
        const pulse = p.online && !reducedMotion() && document.visibilityState === "visible";
        if (pulse) animate = true;
        playerDisc(ctx, sx, sy, p, colorOf(t, tokenOf(p.xuid)), t, on, pulse);
        if (v.s >= 0.25 || p.online || on) {
          labels.push({
            txt: p.online ? p.gamertag : `${p.gamertag} · offline`,
            x: sx,
            y: sy - 8,
            pr: on ? -1 : p.online ? 0 : 1,
          });
        }
        nextHits.push({ kind: "player", sx, sy, p });
      }
    }

    // the tapped marker, on top with its ring and label
    for (const m of tapped) {
      markerGlyph(ctx, m, t, true);
      const txt = markerLabel(m);
      if (txt) labels.push({ txt, x: m.sx, y: m.sy - 8, pr: -1 });
      nextHits.push({ kind: "marker", sx: m.sx, sy: m.sy, m });
    }

    // the tapped or pinned spot
    if (sel?.kind === "spot" && sel.dim === st.dim) {
      const [sx, sy] = toScreen(v, f, sel.x + 0.5, sel.z + 0.5);
      spotMark(ctx, sx, sy, v.s, sel.pinned, t);
    }

    const scale = scaleRect(ctx, v.s, f);
    placeLabels(ctx, labels, keepOuts(el, f, scale), f, t);
    drawScale(ctx, scale, t);
    hits.current = nextHits;
    if (animate) dirty.current = true;
  }, [want]);

  useEffect(() => {
    let raf = 0;
    const loop = () => {
      if (dirty.current) {
        dirty.current = false;
        draw();
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [draw]);

  // ---- the panel's height: peek, the layer grid, or 80% ----

  // No deps: re-measured after every render, since anything in the panel can move its edge.
  useLayoutEffect(() => {
    const root$ = root.current;
    const head = dragEl.current;
    if (!root$ || !head) return;
    const H = root$.getBoundingClientRect().height || window.innerHeight;
    const base = head.offsetHeight + (selEl.current?.offsetHeight ?? 0);
    let h = base + 1;
    if (snap === "mid" && tab === "time") {
      // The Timeline's working set — days, sessions, transport, scrubber — and no further;
      // the map keeps at least 190 px.
      const sc = scrubEl.current;
      const end = sc ? sc.offsetTop + sc.offsetHeight + 8 : base + 1;
      h = Math.min(end, Math.max(base + 1, H - 190));
    } else if (snap === "mid") {
      const g = gridEl.current;
      const gridBottom = g ? g.offsetTop + g.offsetHeight + 14 : 0;
      h = Math.min(base + gridBottom, Math.max(base + 1, H - 190));
    } else if (snap === "open") {
      h = H * 0.8;
    }
    h = Math.round(Math.min(h, H * 0.8));
    if (h !== panelH) setPanelH(h);
    const visible = H - h;
    if (visible !== vis) setVis(visible);
    frame.current = { ...frame.current, bottom: h, top: visible < TINY_PX ? 56 : 64 };
    redraw();
  });

  // ---- gestures ----

  const ptrs = useRef(new Map<number, { x: number; y: number; x0: number; y0: number }>());
  const gesture = useRef({
    moved: false,
    longed: false,
    pinch: null as { d: number; s: number } | null,
  });
  const lp = useRef<ReturnType<typeof setTimeout> | null>(null);

  const local = (e: { clientX: number; clientY: number }): [number, number] => {
    const r = mapEl.current?.getBoundingClientRect();
    return [e.clientX - (r?.left ?? 0), e.clientY - (r?.top ?? 0)];
  };

  function onPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    if ((e.target as Element).closest("button")) return;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY });
    const g = gesture.current;
    g.moved = false;
    g.longed = false;
    if (lp.current) clearTimeout(lp.current);
    if (ptrs.current.size === 2) {
      const [a, b] = [...ptrs.current.values()];
      if (a && b) g.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), s: view.current.s };
      return;
    }
    const [sx, sy] = local(e);
    lp.current = setTimeout(() => {
      if (g.moved || ptrs.current.size !== 1) return;
      g.longed = true;
      dropPin(sx, sy);
    }, LONG_PRESS_MS);
  }

  function onPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    const p = ptrs.current.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x;
    const dy = e.clientY - p.y;
    p.x = e.clientX;
    p.y = e.clientY;
    const g = gesture.current;
    if (Math.hypot(p.x - p.x0, p.y - p.y0) > MOVE_SLOP_PX) {
      g.moved = true;
      if (lp.current) clearTimeout(lp.current);
    }
    if (ptrs.current.size >= 2 && g.pinch) {
      const [a, b] = [...ptrs.current.values()];
      if (!a || !b) return;
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const [mx, my] = local({ clientX: (a.x + b.x) / 2, clientY: (a.y + b.y) / 2 });
      const factor = (g.pinch.s * dist) / g.pinch.d / view.current.s;
      view.current = panBy(zoomAt(view.current, frame.current, factor, mx, my), dx / 2, dy / 2);
      redraw();
    } else if (ptrs.current.size === 1 && g.moved) {
      view.current = panBy(view.current, dx, dy);
      // Dragging the map away from the followed player stops following them.
      if (state.current.tl.follow) {
        state.current.tl = { ...state.current.tl, follow: false };
        setTl((c) => ({ ...c, follow: false }));
      }
      redraw();
    }
  }

  function onPointerUp(e: ReactPointerEvent<HTMLDivElement>) {
    const p = ptrs.current.get(e.pointerId);
    if (!p) return;
    ptrs.current.delete(e.pointerId);
    const g = gesture.current;
    if (ptrs.current.size < 2) g.pinch = null;
    if (lp.current) clearTimeout(lp.current);
    if (!g.moved && !g.longed && ptrs.current.size === 0 && e.type === "pointerup") {
      const [sx, sy] = local(e);
      tap(sx, sy);
    }
  }

  useEffect(() => {
    const el = mapEl.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const [sx, sy] = [e.clientX - r.left, e.clientY - r.top];
      view.current = zoomAt(view.current, frame.current, e.deltaY < 0 ? 1.25 : 0.8, sx, sy);
      redraw();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [redraw]);

  function selectMarker(m: Marker) {
    if (m.type === "struct") select({ kind: "struct", s: m.s, dim });
    else if (m.type === "event") select({ kind: "event", e: m.e });
    else select({ kind: "spawn", x: m.x, z: m.z });
  }

  function tap(sx: number, sy: number) {
    const hit = hitTest(hits.current, sx, sy);
    if (hit?.kind === "cluster") {
      const to = clusterTarget(hit.members, view.current.s);
      const first = hit.members[0];
      if (to) glide(to);
      else if (first) selectMarker(first);
      return;
    }
    if (hit?.kind === "player") {
      // On the timeline, the tapped player is who Follow follows.
      if (tl.on) setTl((c) => ({ ...c, focus: hit.p.xuid }));
      return select({ kind: "player", p: hit.p, at: tl.on && !tl.live ? tRef.current : undefined });
    }
    if (hit?.kind === "marker") return selectMarker(hit.m);
    const [wx, wz] = toWorld(view.current, frame.current, sx, sy);
    const x = Math.floor(wx);
    const z = Math.floor(wz);
    if (sel?.kind === "spot" && sel.x === x && sel.z === z) return select(null);
    select({ kind: "spot", x, z, dim, pinned: false });
  }

  function dropPin(sx: number, sy: number) {
    const [wx, wz] = toWorld(view.current, frame.current, sx, sy);
    const x = Math.floor(wx);
    const z = Math.floor(wz);
    select({ kind: "spot", x, z, dim, pinned: true });
    glide({ x: x + 0.5, z: z + 0.5, s: Math.max(view.current.s, PIN_SCALE) });
    say("Pin dropped — nudge it block by block");
  }

  function nudge(dx: number, dz: number) {
    if (sel?.kind !== "spot") return;
    const next = { ...sel, x: sel.x + dx, z: sel.z + dz };
    setSel(next);
    view.current = { ...view.current, x: next.x + 0.5, z: next.z + 0.5 };
    redraw();
  }

  function zoomBy(factor: number) {
    const f = frame.current;
    view.current = zoomAt(view.current, f, factor, f.w / 2, (f.h - f.bottom) / 2 + f.top / 2);
    redraw();
  }

  async function copy(x: number, z: number) {
    const txt = `${x} ${z}`;
    try {
      await navigator.clipboard.writeText(txt);
      say(`Copied X Z: ${txt}`);
    } catch {
      say(`Couldn't reach the clipboard — ${txt}`);
    }
  }

  async function searchAgain() {
    try {
      if (!source.sweep) return;
      const sweep = await source.sweep(slot);
      setStructs((s) => (s ? { ...s, sweep } : s));
    } catch (err) {
      say(errText(err));
    }
  }

  // The panel's top edge drags between its three heights.
  const drag = useRef<{ y0: number; moved: boolean } | null>(null);
  function onDragDown(e: ReactPointerEvent<HTMLDivElement>) {
    drag.current = { y0: e.clientY, moved: false };
    if (!(e.target as Element).closest("button")) e.currentTarget.setPointerCapture?.(e.pointerId);
  }
  function onDragMove(e: ReactPointerEvent<HTMLDivElement>) {
    if (drag.current && Math.abs(e.clientY - drag.current.y0) > 8) drag.current.moved = true;
  }
  function onDragUp(e: ReactPointerEvent<HTMLDivElement>) {
    const d = drag.current;
    drag.current = null;
    if (!d?.moved) return;
    const order: Snap[] = ["min", "mid", "open"];
    const i = Math.min(2, Math.max(0, order.indexOf(snap) + (e.clientY < d.y0 ? 1 : -1)));
    setSnap(order[i] ?? "min");
  }

  // The card's swipe-down would close all of Minecraft; on the map a drag is a pan.
  const stop = (e: TouchEvent) => e.stopPropagation();

  const now = nowS();
  const days = dayStrip(
    sessions,
    now,
    roster.map((r) => r.xuid),
  );
  // Anywhere but now the line leads with Not live, whatever tab is open.
  const status = timeTravel
    ? timeStatus(tShown, now, tl.playing, tl.speed)
    : statusLine({
        live,
        loaded,
        online: onlineCount,
        worldName: slotRec ? slotLabel(slotRec) : slot,
        lastPlayed: slotRec?.last_played ?? null,
        unreachable,
        tilesVisible: tiles.visible,
        tilesPending: tiles.pending,
        dim,
        explored: info ? info.extent !== null : true,
      });

  const short = vis < SHORT_PX;
  const tiny = vis < TINY_PX;
  const title = slotRec ? slotLabel(slotRec) : slot;

  return (
    <div
      className={`subscreen mc-layer mc-map${short ? " short" : ""}${tiny ? " tiny" : ""}`}
      ref={root}
      aria-hidden={covered || undefined}
      onTouchStart={stop}
      onTouchMove={stop}
    >
      <div
        className="mc-map-view"
        ref={mapEl}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onContextMenu={(e) => e.preventDefault()}
        role="application"
        aria-label={`Map of ${title}, ${dimName(dim)}. Drag to pan, pinch or use the buttons to zoom, tap a spot for what is there.`}
      >
        <canvas ref={canvas} className="mc-map-canvas" />
      </div>

      <div className="mc-map-top">
        {pop ? (
          <button type="button" className="mc-map-fab" onClick={pop} aria-label="Back" ref={back}>
            <ChevronLeftIcon size={20} />
          </button>
        ) : (
          <span className="mc-map-viewonly">
            <EyeIcon size={16} />
            View only
          </span>
        )}
        <div className="mc-seg mc-map-dims" role="radiogroup" aria-label="Dimension">
          {DIMS.map((d) => (
            <button
              key={d.id}
              type="button"
              // biome-ignore lint/a11y/useSemanticElements: a styled segment; a native radio can't carry this 44px button layout.
              role="radio"
              aria-checked={dim === d.id}
              onClick={() => changeDim(d.id)}
            >
              {d.label}
            </button>
          ))}
        </div>
        {owner && (
          <button
            type="button"
            className="mc-map-fab"
            aria-label="Share this map"
            onClick={() => setSharing(true)}
          >
            <LinkIcon size={20} />
          </button>
        )}
      </div>
      <div className="mc-map-fabs">
        <button
          type="button"
          className="mc-map-fab mc-map-zoom"
          aria-label="Zoom in"
          onClick={() => zoomBy(1.6)}
        >
          <PlusIcon size={20} />
        </button>
        <button
          type="button"
          className="mc-map-fab mc-map-zoom"
          aria-label="Zoom out"
          onClick={() => zoomBy(1 / 1.6)}
        >
          <MinusIcon size={20} />
        </button>
        <button
          type="button"
          className="mc-map-fab"
          aria-label="Show everyone"
          onClick={() => fitEveryone(true)}
        >
          <LocateIcon size={20} />
        </button>
      </div>

      <div className="mc-map-panel" style={{ height: panelH }} data-snap={snap}>
        <div
          className="mc-map-drag"
          ref={dragEl}
          onPointerDown={onDragDown}
          onPointerMove={onDragMove}
          onPointerUp={onDragUp}
        >
          <div className="mc-map-grip" aria-hidden="true" />
          <div className="mc-map-head">
            {owner ? (
              <button
                type="button"
                className="mc-map-world"
                aria-label={`World: ${title} — change`}
                onClick={() => setPicker(true)}
              >
                {title}
                <ChevronDownIcon size={16} />
              </button>
            ) : (
              <span className="mc-map-world">{title}</span>
            )}
            <div className="mc-seg mc-map-tabs" role="tablist" aria-label="Panel">
              <button
                type="button"
                role="tab"
                aria-selected={tab === "layers"}
                aria-checked={tab === "layers"}
                onClick={() => setTab("layers")}
              >
                <LayersIcon size={16} />
                Layers
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "time"}
                aria-checked={tab === "time"}
                onClick={openTimeline}
              >
                <ClockIcon size={16} />
                Timeline
              </button>
            </div>
          </div>
          <div className="mc-map-stat">
            <output className={`mc-map-status${timeTravel ? " time" : ""}`}>
              {status.tone === "spin" ? (
                <span className="mc-map-spin" aria-hidden="true" />
              ) : (
                <span className={`mc-dot mc-dot-${status.tone}`} aria-hidden="true" />
              )}
              <span className="mc-map-status-tx">
                <b>{status.head}</b>
                <span className="mc-map-status-sub">{status.sub}</span>
              </span>
            </output>
            {timeTravel && tab === "layers" && (
              <button type="button" className="mc-btn mc-btn-go mc-map-live" onClick={backToLive}>
                Back to live
              </button>
            )}
            <button
              type="button"
              className="mc-icon-btn mc-map-chev"
              aria-label={snap === "open" ? "Show less" : "Show more"}
              onClick={() => setSnap(snap === "open" ? "min" : snap === "min" ? "mid" : "open")}
            >
              {snap === "open" ? <ChevronDownIcon size={20} /> : <ChevronUpIcon size={20} />}
            </button>
          </div>
        </div>
        <div ref={selEl}>
          {sel && (
            <div className="mc-map-sel">
              <Detail
                sel={sel}
                point={point}
                nowMs={host.nowMs}
                compact={tl.on}
                onClose={() => select(null)}
                onCopy={(x, z) => void copy(x, z)}
                onNudge={nudge}
                onFollow={followPlayer}
                onSessions={(x) => setList({ who: x })}
                onReplay={sessions.length ? replayBefore : null}
                tricorder={tricorder}
                playerToken={
                  sel.kind === "player"
                    ? (roster.find((r) => r.xuid === sel.p.xuid)?.token ?? "--steel")
                    : "--steel"
                }
              />
            </div>
          )}
        </div>
        <div className="mc-map-body">
          {tab === "time" ? (
            <TimelinePanel
              tl={tl}
              t={tShown}
              now={now}
              days={days}
              sessions={sessions}
              events={rangeLog?.events ?? []}
              roster={roster}
              trackWidth={trackW}
              refs={{ thumb: thumbEl, fill: fillEl, clock: clockEl, day: dayEl, track: trackEl }}
              scrubEl={scrubEl}
              onDay={showDay}
              onLoad={loadSession}
              onPlay={togglePlay}
              onSpeed={() => setTl((c) => ({ ...c, speed: nextSpeed(c.speed) }))}
              onFollow={toggleFollow}
              onNow={goNow}
              onScrub={scrubTo}
              onMark={jumpToEvent}
              onPreset={setPreset}
              onToggle={(x) =>
                setTl((c) => ({
                  ...c,
                  hidden: c.hidden.includes(x) ? c.hidden.filter((h) => h !== x) : [...c.hidden, x],
                }))
              }
              onSessions={() => setList({ who: null })}
            />
          ) : (
            <>
              {problem && (
                <div className="mc-map-notice bad">
                  <InfoIcon size={16} />
                  <span>{problem}</span>
                </div>
              )}
              {unreachable && (
                <div className="mc-map-notice bad">
                  <CloudOffIcon size={16} />
                  <span>
                    <b>Can&apos;t reach the box.</b> Pan and zoom still work over the tiles this
                    phone already has; the rest fills in when it&apos;s back.
                  </span>
                </div>
              )}
              {loaded && !live && !unreachable && (
                <div className="mc-map-notice warn">
                  <InfoIcon size={16} />
                  <span>
                    <b>Nobody can be on while it&apos;s stopped,</b> so dots are hollow: where each
                    player logged off.
                  </span>
                </div>
              )}
              {info && dim === "overworld" && (info.extent?.chunks ?? 0) < 64 && (
                <div className="mc-map-notice info">
                  <SatelliteIcon size={16} />
                  <span>
                    <b>Hardly played yet.</b>{" "}
                    {info.extent ? `${info.extent.chunks} chunks around spawn are explored; ` : ""}
                    everything else is the satellite&apos;s survey, predicted from the seed. Pan
                    anywhere.
                  </span>
                </div>
              )}
              <Layers
                gridRef={gridEl}
                ov={ov}
                setOv={setOv}
                layer={layer}
                setLayer={setLayer}
                structs={structs}
                canSearch={live && !sweeping && !!source.sweep}
                onSearch={() => void searchAgain()}
                dim={dim}
                spawn={dim === "overworld" ? !!info?.spawn : null}
                roster={roster}
                exBy={exBy}
                setExBy={setExBy}
                fog={fog}
              />
            </>
          )}
        </div>
      </div>

      {picker && (
        <Sheet title="Map of…" onClose={() => setPicker(false)}>
          <p className="mc-note mc-map-picker-note">
            Any world slot. Only the loaded one shows live positions.
          </p>
          <div className="mc-map-worlds">
            {host.slots.map((s) => (
              <button
                key={s.id}
                type="button"
                className="mc-navrow"
                aria-current={s.id === slot}
                disabled={!s.exists}
                onClick={() => changeSlot(s.id)}
              >
                <span className="mc-navico" aria-hidden="true">
                  <GlobeIcon size={20} />
                </span>
                <span className="mc-navtx">
                  <span className="ops-card-title">{slotLabel(s)}</span>
                  <span className="mc-navmeta">
                    Slot {s.id.replace(/\D/g, "")} · {slotLine(s, host.running)}
                  </span>
                </span>
                {s.active && <span className="badge ok mc-loaded">loaded</span>}
              </button>
            ))}
          </div>
        </Sheet>
      )}
      {sharing && owner && (
        <ShareSheet
          slot={slot}
          world={title}
          players={players?.players ?? []}
          nowMs={host.nowMs}
          onClose={() => setSharing(false)}
        />
      )}
      {list && (
        <SessionsSheet
          world={title}
          sessions={sessions}
          roster={roster}
          who={list.who}
          cur={tl.on ? tl.cur : null}
          now={now}
          onWho={(x) => setList({ who: x })}
          onLoad={loadSession}
          onClose={() => setList(null)}
        />
      )}
      {toast && (
        <output className="toast mc-toast mc-map-toast" style={{ bottom: panelH + 12 }}>
          {toast}
        </output>
      )}
    </div>
  );
}

// ---- what you tapped ----

function Detail({
  sel,
  point,
  nowMs,
  compact,
  onClose,
  onCopy,
  onNudge,
  onFollow,
  onSessions,
  onReplay,
  tricorder,
  playerToken: tok,
}: {
  sel: Sel;
  point: MinecraftMapPoint | null;
  nowMs: number;
  /** On the timeline: one row, so the strip, transport and scrubber keep their room. */
  compact: boolean;
  onClose: () => void;
  onCopy: (x: number, z: number) => void;
  onNudge: (dx: number, dz: number) => void;
  onFollow: (xuid: string) => void;
  onSessions: (xuid: string) => void;
  onReplay: ((e: MinecraftTimelineEvent) => void) | null;
  /** The selected player's Tricorder line, when Dave has it pointed somewhere. */
  tricorder: string | null;
  playerToken: string;
}) {
  let icon: ReactNode;
  let title: string;
  let sub: string;
  let x: number;
  let z: number;
  let y: number | null = null;
  const now = nowMs / 1000;
  const where: MinecraftDim =
    sel.kind === "player"
      ? sel.p.dim
      : sel.kind === "event"
        ? (sel.e.dim ?? "overworld")
        : sel.kind === "spawn"
          ? "overworld"
          : sel.dim;
  if (sel.kind === "spot") {
    const t = spotText(point, sel.dim);
    icon = (
      <span className="mc-map-det-ic" aria-hidden="true">
        {t.survey ? <SatelliteIcon size={18} /> : <CrosshairIcon size={18} />}
      </span>
    );
    title = t.title;
    sub = t.sub;
    x = sel.x;
    z = sel.z;
    y = t.y;
  } else if (sel.kind === "struct") {
    const s = structureInfo(sel.s.structure);
    const ground =
      point === null
        ? ""
        : point.source === "explored"
          ? " · explored ground"
          : " · in the survey, not explored yet";
    icon = (
      <span className="mc-map-det-ic st" aria-hidden="true">
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          width="18"
          height="18"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d={GLYPHS[s.glyph]} />
        </svg>
      </span>
    );
    title = s.label;
    sub = `Located by the server${ground} · ${dimName(sel.dim)}`;
    x = sel.s.x;
    z = sel.s.z;
  } else if (sel.kind === "event") {
    const e = sel.e;
    const glyph =
      e.kind === "death" ? (
        <SkullIcon size={18} />
      ) : e.kind === "join" ? (
        <LogInIcon size={18} />
      ) : e.kind === "leave" ? (
        <LogOutIcon size={18} />
      ) : (
        <PortalIcon size={18} />
      );
    icon = (
      <span className={`mc-map-det-ic${e.kind === "death" ? " death" : ""}`} aria-hidden="true">
        {glyph}
      </span>
    );
    title = eventWords(e);
    sub = `${fmtDay(e.at, now)}, ${fmtClock(e.at)} · ${
      e.kind === "dimension" ? "through the portal here" : dimName(where)
    }`;
    x = Math.round(e.x ?? 0);
    z = Math.round(e.z ?? 0);
    y = e.y === null ? null : Math.round(e.y);
  } else if (sel.kind === "spawn") {
    icon = (
      <span className="mc-map-det-ic" aria-hidden="true">
        <FlagIcon size={18} />
      </span>
    );
    title = "World spawn";
    sub = "Overworld · where new players start";
    x = sel.x;
    z = sel.z;
  } else {
    const p = sel.p;
    icon = (
      <span className="mc-map-det-ic pl" style={{ background: `var(${tok})` }} aria-hidden="true">
        {initialOf(p.gamertag)}
      </span>
    );
    title = p.gamertag;
    sub =
      sel.at !== undefined
        ? `here at ${fmtClock(sel.at)}, ${fmtDay(sel.at, now).toLowerCase()} · ${dimName(p.dim)}`
        : p.online
          ? `online · here now · ${dimName(p.dim)}`
          : `offline · logged off here ${agoOf(p.at, nowMs)}`;
    x = Math.round(p.x);
    z = Math.round(p.z);
    y = Math.round(p.y);
  }
  if (compact) {
    return (
      <div className="mc-map-det-row">
        {icon}
        <div className="mc-map-det-tx">
          <div className="mc-map-det-t">{title}</div>
          <div className="mc-map-det-s mc-num">
            X {x} · Z {z}
          </div>
        </div>
        <button
          type="button"
          className="mc-btn mc-btn-primary mc-map-copy1"
          aria-label={`Copy X Z ${x} ${z}`}
          onClick={() => onCopy(x, z)}
        >
          <CopyIcon size={18} />
        </button>
        <button type="button" className="mc-icon-btn" aria-label="Close" onClick={onClose}>
          <XIcon size={20} />
        </button>
      </div>
    );
  }
  const nether = where === "nether";
  const [ox, oz] = overworldSide(x, z);
  return (
    <div className="mc-map-det">
      <div className="mc-map-det-h">
        {icon}
        <div className="mc-map-det-tx">
          <div className="mc-map-det-t">{title}</div>
          <div className="mc-map-det-s">{sub}</div>
        </div>
        <button type="button" className="mc-icon-btn" aria-label="Close" onClick={onClose}>
          <XIcon size={20} />
        </button>
      </div>
      <div className="mc-map-coord">
        <div className="mc-map-xyz">
          <span>
            <em>X</em>
            {x}
          </span>
          {y !== null && (
            <span>
              <em>Y</em>
              {y}
            </span>
          )}
          <span>
            <em>Z</em>
            {z}
          </span>
        </div>
        <button type="button" className="mc-btn mc-btn-primary" onClick={() => onCopy(x, z)}>
          <CopyIcon size={18} />
          Copy X Z
        </button>
      </div>
      {sel.kind === "spot" && sel.pinned && (
        <fieldset className="mc-map-nudge" aria-label="Nudge the pin one block">
          <span className="mc-note">Nudge one block</span>
          <NudgeBtn label="West" onClick={() => onNudge(-1, 0)} icon="w" />
          <NudgeBtn label="North" onClick={() => onNudge(0, -1)} icon="n" />
          <NudgeBtn label="South" onClick={() => onNudge(0, 1)} icon="s" />
          <NudgeBtn label="East" onClick={() => onNudge(1, 0)} icon="e" />
        </fieldset>
      )}
      {nether && (
        <div className="mc-map-alt">
          Overworld side:{" "}
          <b>
            X {ox} · Z {oz}
          </b>
        </div>
      )}
      {tricorder && !compact && (
        <p className="mc-map-det-tri">
          <CrosshairIcon size={16} />
          <span>
            <b>Tricorder</b> · {tricorder}
          </span>
        </p>
      )}
      {sel.kind === "player" && (
        <div className="mc-btnrow mc-map-det-acts">
          <button type="button" className="mc-btn" onClick={() => onFollow(sel.p.xuid)}>
            <LocateIcon size={18} />
            Follow
          </button>
          <button type="button" className="mc-btn" onClick={() => onSessions(sel.p.xuid)}>
            <ClockIcon size={18} />
            Sessions
          </button>
        </div>
      )}
      {sel.kind === "event" && sel.e.kind === "death" && onReplay && (
        <div className="mc-btnrow mc-map-det-acts">
          <button type="button" className="mc-btn" onClick={() => onReplay(sel.e)}>
            <RewindIcon size={18} />
            Replay the minutes before
          </button>
        </div>
      )}
    </div>
  );
}

function NudgeBtn({
  label,
  onClick,
  icon,
}: { label: string; onClick: () => void; icon: "w" | "n" | "s" | "e" }) {
  const d = {
    w: "m12 19-7-7 7-7M19 12H5",
    n: "m5 12 7-7 7 7M12 19V5",
    s: "M12 5v14M19 12l-7 7-7-7",
    e: "M5 12h14M12 5l7 7-7 7",
  }[icon];
  return (
    <button
      type="button"
      className="mc-icon-btn mc-map-nudge-btn"
      aria-label={label}
      onClick={onClick}
    >
      <svg
        viewBox="0 0 24 24"
        width="20"
        height="20"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d={d} />
      </svg>
    </button>
  );
}

// ---- the Layers tab ----

function Layers({
  gridRef,
  ov,
  setOv,
  layer,
  setLayer,
  structs,
  canSearch,
  onSearch,
  dim,
  spawn,
  roster,
  exBy,
  setExBy,
  fog,
}: {
  gridRef: React.RefObject<HTMLDivElement>;
  ov: Overlays;
  setOv: (f: (o: Overlays) => Overlays) => void;
  layer: BaseLayer;
  setLayer: (l: BaseLayer) => void;
  structs: MinecraftMapStructures | null;
  canSearch: boolean;
  onSearch: () => void;
  dim: MinecraftDim;
  /** Whether the box knows the world spawn (from level.dat); null outside the Overworld. */
  spawn: boolean | null;
  roster: RosterEntry[];
  exBy: string | null;
  setExBy: (x: string | null) => void;
  fog: MinecraftExplored | null;
}) {
  const forest: [number, number, number] = [5, 102, 33];
  const plains: [number, number, number] = [141, 179, 96];
  const key = dim === "overworld" ? forest : dim === "nether" ? [191, 59, 59] : [128, 128, 255];
  const sweep = structs?.sweep;
  const structSub =
    sweep?.state === "running"
      ? `searching the world · ${Math.round(((sweep.done ?? 0) / Math.max(1, sweep.total ?? 1)) * 100)}%`
      : structs?.swept_at
        ? "from the server’s locate · exact"
        : "not searched yet — needs this world loaded and running";
  const toggle = (k: keyof typeof ov) => setOv((o) => ({ ...o, [k]: !o[k] }));
  return (
    <>
      <h3 className="mc-sect mc-map-sect">Map</h3>
      <div className="mc-map-ly-grid" role="radiogroup" aria-label="Base layer" ref={gridRef}>
        {BASE_LAYERS.map(([id, t, s, sw]) => (
          <button
            key={id}
            type="button"
            className="mc-map-ly"
            // biome-ignore lint/a11y/useSemanticElements: a styled option card; a native radio can't carry this layout.
            role="radio"
            aria-checked={layer === id}
            onClick={() => setLayer(id)}
          >
            <span
              className="mc-map-sw"
              style={{
                background: `linear-gradient(135deg,rgb(${sw.join(",")}) 0 50%,rgb(${asSurvey(plains)}) 50%)`,
              }}
              aria-hidden="true"
            />
            <span className="mc-map-ly-tx">
              <span className="mc-map-ly-t">{t}</span>
              <span className="mc-map-ly-s">{s}</span>
            </span>
          </button>
        ))}
        {[["Height", "contours"]].map(([t, s]) => (
          <button
            key={t}
            type="button"
            className="mc-map-ly"
            // biome-ignore lint/a11y/useSemanticElements: a styled option card; a native radio can't carry this layout.
            role="radio"
            aria-checked="false"
            disabled
          >
            <span className="mc-map-sw later" aria-hidden="true" />
            <span className="mc-map-ly-tx">
              <span className="mc-map-ly-t">{t}</span>
              <span className="mc-map-ly-s">{s} · later</span>
            </span>
          </button>
        ))}
      </div>

      <h3 className="mc-sect mc-map-sect">On the map</h3>
      <div>
        <OverlayRow
          icon={<UserIcon size={18} />}
          title="Players"
          sub="last positions · live while on"
          on={ov.players}
          onToggle={() => toggle("players")}
        />
        <OverlayRow
          icon={<RouteIcon size={18} />}
          title="Trails"
          sub="each player's latest session · on the timeline, up to the moment"
          on={ov.trails}
          onToggle={() => toggle("trails")}
        />
        <OverlayRow
          icon={<SkullIcon size={18} />}
          title="Deaths"
          sub="the last week's · tap a skull for the cause"
          on={ov.deaths}
          onToggle={() => toggle("deaths")}
        />
        <OverlayRow
          icon={<BedIcon size={18} />}
          title="Beds & spawn"
          sub={
            spawn === false
              ? "the box hasn't read this world's spawn · beds aren't recorded yet"
              : "the world spawn, in the Overworld · beds aren't recorded yet"
          }
          on={ov.spawn}
          onToggle={() => toggle("spawn")}
        />
        <OverlayRow
          icon={<CastleIcon size={18} />}
          title="Structures"
          sub={structSub}
          on={ov.structures}
          onToggle={() => toggle("structures")}
        >
          {canSearch && (
            <button type="button" className="mc-link mc-map-search" onClick={onSearch}>
              Search again
            </button>
          )}
        </OverlayRow>
        <OverlayRow
          icon={<EyeIcon size={18} />}
          title="Explored by…"
          sub={
            fog?.truncated
              ? "the travel log’s fog of war · the first 60,000 chunks"
              : "the travel log’s fog of war · lifts as the timeline plays"
          }
          on={ov.explored}
          onToggle={() => toggle("explored")}
        />
        {ov.explored && (
          <div className="mc-chips mc-map-who" role="radiogroup" aria-label="Explored by">
            {[null, ...roster.map((r) => r.xuid)].map((x) => {
              const r = roster.find((q) => q.xuid === x);
              return (
                <button
                  key={x ?? "anyone"}
                  type="button"
                  className="mc-chip"
                  // biome-ignore lint/a11y/useSemanticElements: a styled chip; a native radio can't carry this layout.
                  role="radio"
                  aria-checked={exBy === x}
                  onClick={() => setExBy(x)}
                >
                  {r && (
                    <span
                      className="pd"
                      style={{ background: `var(${r.token})` }}
                      aria-hidden="true"
                    />
                  )}
                  {r ? r.gamertag : "Anyone"}
                </button>
              );
            })}
          </div>
        )}
        <OverlayRow
          icon={<GridIcon size={18} />}
          title="Grid & coordinates"
          sub=""
          on={ov.grid}
          onToggle={() => toggle("grid")}
        />
        {(
          [
            [<PinIcon key="p" size={18} />, "Saved places", "with Minecraft_Dave's places"],
            [<PortalIcon key="g" size={18} />, "Wormhole gates", "comes with M12"],
          ] as const
        ).map(([icon, t, s]) => (
          <div className="mc-map-ov later" key={t}>
            <span className="mc-map-ov-gl" aria-hidden="true">
              {icon}
            </span>
            <span className="mc-map-ov-t">
              {t}
              <small>{s}</small>
            </span>
            <span className="badge off">later</span>
          </div>
        ))}
      </div>

      <h3 className="mc-sect mc-map-sect">Legend</h3>
      <div className="mc-map-lg">
        <div className="mc-map-lg-row">
          <span className="mc-map-lg-i">
            <i style={{ background: `rgb(${key.join(",")})` }} />
            Explored
          </span>
          <span className="mc-map-lg-i">
            <i style={{ background: `rgb(${asSurvey(key as [number, number, number])})` }} />
            Satellite survey
          </span>
        </div>
        <p className="mc-note">
          Explored ground is drawn in full colour. Everything else is the satellite&apos;s survey —
          predicted from the seed, half-desaturated and dimmed.{" "}
          {dim === "the_end"
            ? "Black is the void."
            : "It matches the real world at about 97% of blocks."}
        </p>
        <div className="mc-map-lg-row">
          {LEGEND[dim].map(([name, c]) => (
            <span className="mc-map-lg-i" key={name}>
              <i style={{ background: `rgb(${c.join(",")})` }} />
              {name}
            </span>
          ))}
        </div>
        <p className="mc-note">
          {layer === "surface" && dim !== "nether"
            ? "Explored ground in the colour of its top block, seen from above, hillshaded from the north-west. Water darkens with depth; the survey is biome colours."
            : "Biome colours, hillshaded from the north-west. Water darkens with depth."}
          {layer === "surface" && dim === "nether"
            ? " The Nether is shown by biome: from above, its top block is the bedrock roof."
            : ""}
        </p>
        <div className="mc-map-lg-row">
          <span className="mc-map-lg-i">
            <UserIcon size={14} />
            Player
          </span>
          <span className="mc-map-lg-i">
            <CastleIcon size={14} />
            Structure
          </span>
          <span className="mc-map-lg-i">
            <SkullIcon size={14} />
            Death
          </span>
          <span className="mc-map-lg-i">
            <FlagIcon size={14} />
            Spawn
          </span>
          <span className="mc-map-lg-i">
            <span className="mc-map-lg-n" aria-hidden="true">
              3
            </span>
            Grouped — tap to zoom in
          </span>
        </div>
        {ov.explored && (
          <div className="mc-map-lg-row">
            <span className="mc-map-lg-i">
              <i className="mc-map-lg-fog" />
              Not explored by {roster.find((r) => r.xuid === exBy)?.gamertag ?? "anyone"} yet
            </span>
          </div>
        )}
      </div>
    </>
  );
}

function OverlayRow({
  icon,
  title,
  sub,
  on,
  onToggle,
  children,
}: {
  icon: ReactNode;
  title: string;
  sub: string;
  on: boolean;
  onToggle: () => void;
  children?: ReactNode;
}) {
  return (
    <div className="mc-map-ov">
      <span className="mc-map-ov-gl" aria-hidden="true">
        {icon}
      </span>
      <span className="mc-map-ov-t">
        {title}
        {sub && <small>{sub}</small>}
        {children}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={title}
        className={`mc-switch${on ? " on" : ""}`}
        onClick={onToggle}
      >
        <span className="mc-knob" />
      </button>
    </div>
  );
}

// ---- canvas drawing ----

/** What labels must stay clear of: the floating controls, the panel and the scale bar. */
function keepOuts(el: HTMLElement, f: Frame, scale: Rect): Rect[] {
  const r0 = el.getBoundingClientRect();
  const out: Rect[] = [scale, { x: 0, y: f.h - f.bottom, w: f.w, h: f.bottom }];
  const page = el.parentElement;
  if (!page) return out;
  for (const e of page.querySelectorAll(".mc-map-top > *, .mc-map-fabs .mc-map-fab")) {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    out.push({ x: r.left - r0.left - 4, y: r.top - r0.top - 4, w: r.width + 8, h: r.height + 8 });
  }
  return out;
}

/** The Minecraft screen's way in: a row under Worlds & backups, glancing who's on the map. */
export function MapEntry() {
  const w = useW();
  const world = w.active ? slotLabel(w.active) : null;
  return (
    <section className="mc-card">
      <button type="button" className="mc-navrow" onClick={() => w.push({ kind: "map" })}>
        <span className="mc-navico" aria-hidden="true">
          <MapIcon size={20} />
        </span>
        <span className="mc-navtx">
          <span className="ops-card-title">Map</span>
          <span className="mc-navmeta">{entryLine(world, w.running ? w.online : [])}</span>
        </span>
        <ChevronRightIcon size={18} />
      </button>
    </section>
  );
}
