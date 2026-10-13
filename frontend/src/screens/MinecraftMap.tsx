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
  type MinecraftBeacon,
  type MinecraftDim,
  type MinecraftExplored,
  type MinecraftGate,
  type MinecraftGoalProgress,
  type MinecraftMapEntities,
  type MinecraftMapInfo,
  type MinecraftMapMarkers,
  type MinecraftMapPlaces,
  type MinecraftMapPlayer,
  type MinecraftMapPlayers,
  type MinecraftMapPoint,
  type MinecraftMapStructures,
  type MinecraftMob,
  type MinecraftPlace,
  type MinecraftSession,
  type MinecraftSlot,
  type MinecraftStructure,
  type MinecraftTimeline,
  type MinecraftTimelineEvent,
  type MinecraftVillager,
  type MinecraftVillagerCard,
} from "../api/client";
import { useBackLayer } from "../backLayers";
import { Sheet } from "../components/Sheet";
import {
  ArrowRightIcon,
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
  PlusIcon,
  PortalIcon,
  RewindIcon,
  RouteIcon,
  SatelliteIcon,
  ShieldCheckIcon,
  ShieldIcon,
  SkullIcon,
  UserIcon,
  XIcon,
} from "../components/icons";
import { agoOf } from "../minecraft";
import {
  type BaseLayer,
  DIMS,
  EXPLORE_REACH,
  type Frame,
  GLYPHS,
  type GlyphKey,
  type MapView,
  PIN_SCALE,
  type Preset,
  type RosterEntry,
  type ScanWords,
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
  gapFrom,
  hitTest,
  initialOf,
  mergeGap,
  nextSpeed,
  overworldSide,
  panBy,
  pathUpTo,
  playersIn,
  posAt,
  presetRange,
  rosterOf,
  scaleOfZoom,
  scanFrame,
  scanPlate,
  scanShown,
  scanWords,
  sessionAt,
  sessionRange,
  slotLabel,
  slotLine,
  spotText,
  statusLine,
  structureInfo,
  tileKey,
  tilesNear,
  timeStatus,
  toScreen,
  toWorld,
  trailBehind,
  trailSince,
  trailTail,
  tricorderLine,
  visibleTiles,
  zoomAt,
} from "../minecraftMap";
import {
  ALL_JOBS,
  type CardLine,
  SPLIT_SCALE,
  VILLAGER_CLUSTER_PX,
  type VillagerFilter,
  entitiesAge,
  filterKey,
  goalWords,
  jobOf,
  matches,
  mobFrom,
  mobWords,
  petChips,
  petNote,
  petsLine,
  petsShown,
  placeGroups,
  placeLines,
  placeSub,
  placesLine,
  rankedPets,
  rankedVillagers,
  spotKey,
  tokenFor,
  tradeTiers,
  tradesNote,
  villageMarks,
  villagerChips,
  villagerTitle,
  villagersLine,
} from "../minecraftMapLayers";
import {
  type Avoid,
  type ListRow,
  beaconKey,
  beaconWords,
  bearingTo,
  gateCenter,
  gateWords,
  listRows,
  overlayLines,
} from "../minecraftMapMarkers";
import type { MapIntent } from "../minecraftMapNav";
import { type MapSource, OWNER_MAP } from "../minecraftMapSource";
import { useForeground } from "../visibility";
import { ShareSheet } from "./MinecraftMapShare";
import { SessionsSheet, TimelinePanel, type TimelineState } from "./MinecraftMapTimeline";
import { ScanLines } from "./MinecraftScanLines";
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
  drawWorldMarkers,
  fetchTile,
  grid,
  line,
  markerGlyph,
  markerLabel,
  markerOwn,
  placeLabels,
  playerDisc,
  reachCircle,
  readTokens,
  reducedMotion,
  scaleRect,
  scanSquare,
  spotMark,
  stars,
  targetMark,
  veil,
  villageTile,
} from "./minecraftMapDraw";

/** Villagers and pets are re-read with the world's files, at most once a minute. */
const ENTITIES_MS = 60_000;
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
  | { kind: "spawn"; x: number; z: number }
  | { kind: "beacon"; b: MinecraftBeacon }
  | { kind: "gate"; g: MinecraftGate }
  | { kind: "place"; p: MinecraftPlace }
  | { kind: "villager"; v: MinecraftVillager; dim: MinecraftDim }
  | { kind: "pet"; m: MinecraftMob; dim: MinecraftDim };

type Hit =
  | { kind: "player"; sx: number; sy: number; p: MinecraftMapPlayer }
  | { kind: "marker"; sx: number; sy: number; m: Marker }
  | { kind: "cluster"; sx: number; sy: number; members: Marker[] };

/** This round's markers (minecraft-map-layers): labelled by the side rule, always. */
const LAYER_TYPES = new Set<Marker["type"]>(["place", "village", "villager", "pet", "named"]);

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
  beacons: boolean;
  gates: boolean;
  places: boolean;
  villagers: boolean;
  pets: boolean;
  explored: boolean;
  grid: boolean;
}

const OVERLAYS: Overlays = {
  players: true,
  trails: true,
  deaths: true,
  spawn: true,
  structures: true,
  // Always drawn while on (variant A): every protected square and every open gate's line.
  beacons: true,
  gates: true,
  // GUI gate minecraft-map-layers (owner, 2026-10-13): places on; villagers and pets off —
  // they are many, they move, and they're as old as the world's last save.
  places: true,
  villagers: false,
  pets: false,
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
  const [ov, setOv] = useState<Overlays>(() =>
    at?.show ? { ...OVERLAYS, [at.show]: true } : OVERLAYS,
  );
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
  const [marks, setMarks] = useState<MinecraftMapMarkers | null>(null);
  const [places, setPlaces] = useState<MinecraftMapPlaces | null>(null);
  const [ents, setEnts] = useState<MinecraftMapEntities | null>(null);
  const [vfilter, setVfilter] = useState<VillagerFilter>(ALL_JOBS);
  const [petsOf, setPetsOf] = useState<string | null>(null);
  const [vcard, setVcard] = useState<MinecraftVillagerCard | "loading" | "failed" | null>(null);
  const [goalsGot, setGoalsGot] = useState<MinecraftGoalProgress | "loading" | "failed" | null>(
    null,
  );
  /** Goal counts read for a place card, by player: counting reads the world's files. */
  const goalCache = useRef(new Map<string, MinecraftGoalProgress>());
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
  // A live scan points the arrow, so the waypoint waiting under it has no line of its own.
  const tricorder = aimer?.target && !aimer.scan ? tricorderLine(aimer, aimer.target) : null;
  const scan = aimer ? scanWords(aimer, host.nowMs / 1000) : null;

  // Everything the draw loop reads, current as of the last render.
  const view = useRef<MapView>(
    at ? { x: at.x + 0.5, z: at.z + 0.5, s: scaleOfZoom(at.zoom) } : { x: 0, z: 0, s: 0.7 },
  );
  const frame = useRef<Frame>({ w: 1, h: 1, bottom: 160, top: 64 });
  /** The scanner whose square the camera framed, kept framed until the map is touched. */
  const reframe = useRef<string | null>(null);
  /** The place whose reach the camera framed, framed again as the panel settles round its
   *  card, until the map is touched. */
  const placeFramed = useRef<MinecraftPlace | null>(null);
  const dirty = useRef(true);
  const hits = useRef<Hit[]>([]);
  const cache = useRef(new Map<string, TileEntry>());
  /** Tiles a trail has just walked through: fetched again, past the browser's cache. */
  const redo = useRef(new Set<string>());
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
    marks,
    places,
    ents,
    vfilter,
    petsOf,
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
    marks,
    places,
    ents,
    vfilter,
    petsOf,
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
  useEffect(redraw, [
    slot,
    dim,
    layer,
    sel,
    ov,
    players,
    structs,
    marks,
    places,
    ents,
    vfilter,
    petsOf,
    info,
    panelH,
    tl,
    log,
    fog,
  ]);

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

  // The world's beacons and gates (§M26): the api's record of the pack's lines, so they
  // draw for a world that isn't loaded too. While it's live they're read again with the
  // trails, which is how a new gate turns up.
  useEffect(() => {
    const read$ = source.markers;
    if (!read$ || !foreground) return;
    let gone = false;
    const read = () =>
      read$(slot)
        .then((m) => {
          if (!gone) setMarks(m);
        })
        .catch(() => {
          if (!gone) setMarks(null);
        });
    void read();
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

  // Every player's saved places (places A): the api's own rows, so they draw for a world
  // that isn't loaded; read again while it's live, as the beacons are.
  useEffect(() => {
    const read$ = source.places;
    if (!read$ || !foreground) return;
    let gone = false;
    const read = () =>
      read$(slot)
        .then((m) => {
          if (!gone) setPlaces(m);
        })
        .catch(() => {
          if (!gone) setPlaces(null);
        });
    void read();
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

  // Villagers, pets and named mobs (§M4e on the map), only while one of their layers is
  // on: the saved world's, read again at most once a minute — the automatic re-read is
  // the only one (owner: no "Read the world again").
  const wantEnts = ov.villagers || ov.pets;
  useEffect(() => {
    const read$ = source.entities;
    if (!read$ || !foreground || !wantEnts) return;
    let gone = false;
    const read = () =>
      read$(slot, dim)
        .then((e) => {
          if (!gone) setEnts(e);
        })
        .catch(() => {
          if (!gone) setEnts(null);
        });
    void read();
    const t = setInterval(read, ENTITIES_MS);
    return () => {
      gone = true;
      clearInterval(t);
    };
  }, [source, slot, dim, wantEnts, foreground]);

  // A tapped villager's trades, by its id; a tapped place's goal counts, once per player
  // while the map is open (counting reads every chest in the world's files).
  const selVillager = sel?.kind === "villager" ? sel.v.id : null;
  useEffect(() => {
    const read$ = source.villager;
    if (!selVillager || !read$) {
      setVcard(null);
      return;
    }
    let gone = false;
    setVcard("loading");
    read$(slot, selVillager)
      .then((c) => {
        if (!gone) setVcard(c);
      })
      .catch(() => {
        if (!gone) setVcard("failed");
      });
    return () => {
      gone = true;
    };
  }, [source, slot, selVillager]);
  const selPlayerOfPlace = sel?.kind === "place" ? sel.p.xuid : null;
  useEffect(() => {
    const read$ = source.goals;
    if (!selPlayerOfPlace || !read$) {
      setGoalsGot(null);
      return;
    }
    const kept = goalCache.current.get(selPlayerOfPlace);
    if (kept) {
      setGoalsGot(kept);
      return;
    }
    let gone = false;
    setGoalsGot("loading");
    read$(selPlayerOfPlace)
      .then((g) => {
        goalCache.current.set(selPlayerOfPlace, g);
        if (!gone) setGoalsGot(g);
      })
      .catch(() => {
        if (!gone) setGoalsGot("failed");
      });
    return () => {
      gone = true;
    };
  }, [source, selPlayerOfPlace]);

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

  // A disc ahead of its trail: someone's newest sample is newer than the log the trails
  // came from. Only the stretch since then is read, for the walkers only, and joined onto
  // the trail, so it reaches the disc instead of a minute behind it; the minute's full
  // read stays the backstop. Once per new sample, so a clock that runs ahead can't keep
  // asking.
  const caughtUp = useRef(0);
  useEffect(() => {
    if (!players || !log || log.world !== players.world || (tl.on && !tl.live)) return;
    const newest = Math.max(...players.players.map((p) => p.at));
    if (!trailBehind(players.players, log) || newest <= caughtUp.current) return;
    caughtUp.current = newest;
    const keep = tl.on ? setRangeLog : setLiveLog;
    const walkers = players.players.filter((p) => p.at > log.now).map((p) => p.xuid);
    let gone = false;
    source
      .timeline(slot, gapFrom(log, walkers), Math.max(log.to, nowS()), {
        include: ["trails", "events"],
        players: walkers,
      })
      .then((got) => {
        if (!gone) keep((was) => (was === log ? mergeGap(log, got) : was));
      })
      .catch(() => {
        // The minute's read brings the trail anyway.
      });
    return () => {
      gone = true;
    };
  }, [source, slot, players, log, tl.on, tl.live]);

  // Explored by: the fog for this dimension, lifted by first-seen time on the timeline. It
  // is read again when the trail has grown (`walkedGen`, below), so the ground just walked
  // is lifted with it.
  const tail = trailTail(log);
  const [walkedGen, setWalkedGen] = useState(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a longer trail reads the fog again.
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
  }, [source, slot, dim, ov.explored, exBy, walkedGen]);

  // And the tiles round the new steps are fetched again: explored ground is baked into them.
  // At most once a minute: the box re-reads the world no oftener, so a faster refresh
  // fetches the same picture. A grown trail inside the minute waits for the next poll.
  const walked = useRef<{ slot: string; tail: number; at: number } | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: each poll looks again at a held refresh.
  useEffect(() => {
    const was = walked.current;
    const now = Date.now();
    if (tail === null || !was || was.slot !== slot) {
      walked.current = tail === null ? null : { slot, tail, at: now };
      return;
    }
    if (tail <= was.tail || now - was.at < TILE_FRESH_MS) return;
    walked.current = { slot, tail, at: now };
    const st = state.current;
    if (st.tl.on && !st.tl.live) return; // a replay's range moved, not anyone's feet
    setWalkedGen((g) => g + 1);
    const near = tilesNear(trailSince(st.log, was.tail, st.dim), EXPLORE_REACH);
    if (!near.size) return;
    for (const [key, e] of cache.current) {
      const [s0, d0, , ...at] = key.split("|");
      if (s0 === slot && d0 === st.dim && e.state === "ok" && near.has(at.join("|"))) {
        redo.current.add(key);
      }
    }
    redraw();
  }, [slot, tail, players, redraw]);

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
    reframe.current = scanShown(p) ? p.xuid : null;
    view.current = (scanShown(p) ? scanFrame(p, frame.current) : null) ?? {
      x: p.x + 0.5,
      z: p.z + 0.5,
      s: FOCUS_SCALE,
    };
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
    setEnts(null);
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
    setMarks(null);
    setPlaces(null);
    setEnts(null);
    goalCache.current.clear();
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
      const due = redo.current.has(key);
      const stale =
        have &&
        ((have.state === "ok" && (due || (live$.current && now - have.at > TILE_FRESH_MS))) ||
          (have.state === "failed" && now - have.at > TILE_RETRY_MS));
      if (have && !stale) return have;
      if (have?.state === "loading" || inFlight.current >= TILES_IN_FLIGHT) return have ?? null;
      const entry: TileEntry = { img: have?.img ?? null, at: now, state: "loading" };
      cache.current.set(key, entry);
      inFlight.current++;
      redo.current.delete(key);
      // The tile route lets the browser keep a tile a minute; a tile the trail just walked
      // through has to come from the box.
      const url: typeof source.tileUrl = due
        ? (...a) => `${source.tileUrl(...a)}&v=${now}`
        : source.tileUrl;
      fetchTile(st.slot, st.dim, t.zoom, t.tx, t.tz, layer, url)
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
    const tokColor = (xuid: string | null, tag: string) => {
      const tok = tokenFor(st.roster, xuid, tag);
      return tok ? colorOf(t, tok) : null;
    };
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
      (m.type === "spawn" && sel?.kind === "spawn") ||
      (m.type === "place" && sel?.kind === "place" && sel.p.id === m.p.id) ||
      (m.type === "villager" && sel?.kind === "villager" && sel.v.id === m.v.id) ||
      ((m.type === "pet" || m.type === "named") && sel?.kind === "pet" && sel.m.id === m.m.id);
    const markers: Marker[] = [];
    const mark = (m: Marker) => {
      if (!off(m.sx, m.sy)) markers.push(m);
    };
    // Beacons and gates (§M26), in the binding order: the protected squares, the gates'
    // lines and the places they lead to, under every tile; their tiles cluster with the
    // point markers below. Not on the replay: they are where the pack last reported them.
    // Saved places (places A): where each one's pin stands, so a gate aimed at that very
    // block ends at the place's pin, not its own violet one (X1). Like the beacons, not on
    // the replay: a place is where it is now.
    const herePlaces =
      st.ov.places && !replay ? (st.places?.places ?? []).filter((p) => p.dim === st.dim) : [];
    const placedAt = new Set(herePlaces.map((p) => spotKey(p.dim, p.x, p.y, p.z)));
    // The tapped place's goal reach, among the lines under every tile (DESIGN draw order).
    if (sel?.kind === "place" && sel.p.dim === st.dim && !replay) {
      reachCircle(ctx, v, f, sel.p, tokColor(sel.p.xuid, sel.p.gamertag), t);
    }
    const world = drawWorldMarkers(ctx, v, f, t, {
      beacons: st.marks?.beacons ?? [],
      gates: st.marks?.gates ?? [],
      dim: st.dim,
      showBeacons: st.ov.beacons && !replay,
      showGates: st.ov.gates && !replay,
      selBeacon: sel?.kind === "beacon" ? beaconKey(sel.b) : null,
      selGate: sel?.kind === "gate" ? sel.g.id : null,
      placedAt,
    });
    const avoid: Avoid = { rects: [...world.avoid.rects], segs: [...world.avoid.segs] };
    // Live scans (§M25): every online scanner's square, over the areas and under the tiles,
    // and the block each found in the Tricorder target's grammar; the bearing only for the
    // tapped one. Not on the replay: a scan isn't recorded.
    if (!replay && st.ov.players && st.players) {
      const tappedP = sel?.kind === "player" ? sel.p.xuid : null;
      for (const p of playersIn(st.players.players, st.dim)) {
        if (!scanShown(p) || !shown(p.xuid) || !p.scan) continue;
        const col = colorOf(t, tokenOf(p.xuid));
        const fd = p.scan.state === "found" ? p.scan.found : null;
        scanSquare(ctx, v, f, p, col, !!fd, t);
        if (!fd || fd.dim !== st.dim) continue;
        const [sx, sy] = toScreen(v, f, fd.x + 0.5, fd.z + 0.5);
        if (tappedP === p.xuid) {
          bearingLine(ctx, v, f, [p.x, p.z], [fd.x + 0.5, fd.z + 0.5], col, t);
          const [px, py] = toScreen(v, f, p.x, p.z);
          // The plate keeps off its own bearing and block too, so neither is ever hidden.
          avoid.segs.push({ ax: px, ay: py, bx: sx, by: sy, own: `scan:${p.xuid}` });
        }
        targetMark(ctx, sx, sy, col, t);
        avoid.rects.push({ x: sx - 12, y: sy - 12, w: 24, h: 24, own: `scan:${p.xuid}` });
      }
    }
    for (const m of world.markers) mark(m);
    for (const p of world.pins) {
      const [x, z] = gateCenter(p.g);
      nextHits.push({
        kind: "marker",
        sx: p.sx,
        sy: p.sy,
        m: { type: "gate", g: p.g, x, z, sx: p.sx, sy: p.sy, pr: 2 },
      });
    }
    labels.push(...world.labels);
    for (const p of herePlaces) {
      const [sx, sy] = toScreen(v, f, p.x + 0.5, p.z + 0.5);
      const tok = tokenFor(st.roster, p.xuid, p.gamertag);
      // A place a gate leads to shares the gate's key: its label may touch its own line.
      const own = p.gates[0] !== undefined ? `g:${p.gates[0]}` : `pl:${p.id}`;
      mark({ type: "place", p, tok, own, x: p.x, z: p.z, sx, sy, pr: 1.5 });
    }
    // Villagers (A): zoomed out, a village is one tile with its count — the located one
    // keeps its amber tile; from SPLIT_SCALE a block, each villager by its job.
    const e = !replay && st.ents?.dimension === st.dim ? st.ents : null;
    const villageStructs = new Set<MinecraftStructure>();
    if (e && st.ov.villagers) {
      if (v.s < SPLIT_SCALE) {
        for (const vm of villageMarks(e, st.structs?.structures ?? [], st.vfilter)) {
          if (vm.struct) villageStructs.add(vm.struct);
          const [sx, sy] = toScreen(v, f, vm.x + 0.5, vm.z + 0.5);
          mark({ type: "village", v: vm, x: vm.x, z: vm.z, sx, sy, pr: 1.7 });
        }
      } else {
        const named = st.vfilter.kind !== "all";
        for (const vv of e.villagers) {
          if (!matches(vv, st.vfilter)) continue;
          const [sx, sy] = toScreen(v, f, vv.x + 0.5, vv.z + 0.5);
          mark({
            type: "villager",
            v: vv,
            label: named ? villagerTitle(vv) : null,
            x: vv.x,
            z: vv.z,
            sx,
            sy,
            pr: 1.8,
            cr: VILLAGER_CLUSTER_PX,
          });
        }
      }
    }
    if (e && st.ov.pets) {
      const shownMobs = petsShown(e, st.petsOf);
      for (const m of shownMobs.pets) {
        const [sx, sy] = toScreen(v, f, m.x + 0.5, m.z + 0.5);
        const tok = m.owner_name ? tokenFor(st.roster, null, m.owner_name) : null;
        mark({ type: "pet", m, tok, x: m.x, z: m.z, sx, sy, pr: 1.6 });
      }
      for (const m of shownMobs.named) {
        const [sx, sy] = toScreen(v, f, m.x + 0.5, m.z + 0.5);
        mark({ type: "named", m, x: m.x, z: m.z, sx, sy, pr: 1.65 });
      }
    }
    if (st.ov.structures && st.structs && st.structs.dimension === st.dim) {
      for (const s of st.structs.structures) {
        if (villageStructs.has(s)) continue;
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
        const own = markerOwn(one);
        if (one.type === "village") {
          // Its count goes on a corner no line crosses, and labels keep off it too.
          const box = villageTile(ctx, one.v, one.sx, one.sy, t, avoid.segs);
          avoid.rects.push({ ...box, own });
        } else markerGlyph(ctx, one, t, false);
        const txt = markerLabel(one);
        // This round's names go to the side, as a gate's place does (M26's rule).
        if (txt && LAYER_TYPES.has(one.type))
          labels.push({ txt, x: one.sx, y: one.sy, pr: 2, own, side: 12 });
        else if (txt && (v.s >= 0.25 || one.type === "spawn"))
          labels.push({ txt, x: one.sx, y: one.sy - 8, pr: one.pr });
        if (own) avoid.rects.push({ x: one.sx - 12, y: one.sy - 12, w: 24, h: 24, own });
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
    // A live scan points the arrow now: the waypoint waiting under it isn't drawn.
    const aim = aimer?.scan ? undefined : aimer?.target;
    if (aimer && aim && aim.dim === st.dim) {
      const col = colorOf(t, tokenOf(aimer.xuid));
      const tx = aim.x + 0.5;
      const tz = aim.z + 0.5;
      const [sx, sy] = toScreen(v, f, tx, tz);
      if (aimer.dim === st.dim) {
        bearingLine(ctx, v, f, [aimer.x, aimer.z], [tx, tz], col, t);
        // A gate's label never sits on the bearing either (README "Labels").
        const [px, py] = toScreen(v, f, aimer.x, aimer.z);
        avoid.segs.push({ ax: px, ay: py, bx: sx, by: sy, own: "tricorder" });
      }
      targetMark(ctx, sx, sy, col, t);
      labels.push({ txt: aim.label, x: sx, y: sy - 14, pr: -1, own: "tricorder" });
    }

    // The tapped beacon or gate, with its ring — still under the players, so nobody is
    // ever hidden by one (README "Draw order").
    if (world.tapped) {
      const m = world.tapped;
      markerGlyph(ctx, m, t, true);
      avoid.rects.push({ x: m.sx - 18, y: m.sy - 18, w: 36, h: 36, own: markerOwn(m) });
      nextHits.push({ kind: "marker", sx: m.sx, sy: m.sy, m });
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
        avoid.rects.push({ x: sx - 12, y: sy - 12, w: 24, h: 24, own: `p:${tr.xuid}` });
        labels.push({ txt: tr.gamertag, x: sx, y: sy - 8, pr: on ? -1 : 0, own: `p:${tr.xuid}` });
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
        avoid.rects.push({ x: sx - 12, y: sy - 12, w: 24, h: 24, own: `p:${p.xuid}` });
        if (v.s >= 0.25 || p.online || on) {
          // A scanner's one plate carries their name first, and falls back to the name.
          const plate = replay ? null : scanPlate(p);
          labels.push({
            txt: plate ?? (p.online ? p.gamertag : `${p.gamertag} · offline`),
            short: plate ? p.gamertag : undefined,
            x: sx,
            y: sy - 8,
            pr: on ? -1 : p.online ? 0 : 1,
            own: `p:${p.xuid}`,
          });
        }
        nextHits.push({ kind: "player", sx, sy, p });
      }
    }

    // the tapped marker, on top with its ring and label; no other label sits on its ring
    for (const m of tapped) {
      markerGlyph(ctx, m, t, true);
      avoid.rects.push({ x: m.sx - 18, y: m.sy - 18, w: 36, h: 36, own: markerOwn(m) });
      const txt =
        m.type === "villager"
          ? villagerTitle(m.v)
          : m.type === "pet"
            ? mobWords(m.m).title
            : markerLabel(m);
      if (txt && LAYER_TYPES.has(m.type))
        labels.push({ txt, x: m.sx, y: m.sy, pr: -1, own: markerOwn(m), side: 18 });
      else if (txt) labels.push({ txt, x: m.sx, y: m.sy - 8, pr: -1 });
      nextHits.push({ kind: "marker", sx: m.sx, sy: m.sy, m });
    }

    // the tapped or pinned spot
    if (sel?.kind === "spot" && sel.dim === st.dim) {
      const [sx, sy] = toScreen(v, f, sel.x + 0.5, sel.z + 0.5);
      spotMark(ctx, sx, sy, v.s, sel.pinned, t);
    }

    const scale = scaleRect(ctx, v.s, f);
    placeLabels(ctx, labels, keepOuts(el, f, scale), f, t, avoid);
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
    const moved = frame.current.bottom !== h;
    frame.current = { ...frame.current, bottom: h, top: visible < TINY_PX ? 56 : 64 };
    // A framed scanner stays framed while the panel settles round their card, until the
    // map is touched.
    const owed = reframe.current;
    const who = owed && sel?.kind === "player" && sel.p.xuid === owed ? aimer : undefined;
    const to = moved && who ? scanFrame(who, frame.current) : null;
    if (to) glide(to);
    const pf = placeFramed.current;
    if (moved && pf && sel?.kind === "place" && sel.p.id === pf.id) {
      const fit = reachFrame(pf, frame.current);
      if (fit) glide(fit);
    }
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
    reframe.current = null;
    placeFramed.current = null;
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
    else if (m.type === "beacon") select({ kind: "beacon", b: m.b });
    else if (m.type === "gate") select({ kind: "gate", g: m.g });
    else if (m.type === "place") selectPlace(m.p);
    else if (m.type === "villager") select({ kind: "villager", v: m.v, dim });
    else if (m.type === "pet" || m.type === "named") select({ kind: "pet", m: m.m, dim });
    else if (m.type === "village") {
      // A village opens into its villagers: zoom in until they split.
      glide({ x: m.x + 0.5, z: m.z + 0.5, s: Math.max(view.current.s, SPLIT_SCALE * 1.2) });
    } else select({ kind: "spawn", x: m.x, z: m.z });
  }

  /** A tapped place, framed with its 48-block reach in the visible map (M26's 72 px
   *  margin), so the circle the card speaks of is always in view (the review's S4). */
  function selectPlace(p: MinecraftPlace) {
    // Into its dimension first: switching dimension clears what's tapped.
    if (p.dim !== state.current.dim) jumpDim(p.dim);
    select({ kind: "place", p });
    placeFramed.current = p;
    const fit = reachFrame(p, frame.current);
    if (fit) glide(fit);
  }

  /** A villager from the ranked list: selected, and its village split round it. */
  function flyToVillager(vv: MinecraftVillager) {
    select({ kind: "villager", v: vv, dim });
    setSnap("min");
    glide({ x: vv.x + 0.5, z: vv.z + 0.5, s: Math.max(view.current.s, SPLIT_SCALE * 1.4) });
  }

  /** A gate or beacon from the Layers list: select it and bring it to the visible map's
   *  middle, clear of the floating controls, in its own dimension. */
  function flyToPet(m: MinecraftMob) {
    select({ kind: "pet", m, dim });
    setSnap("min");
    glide({ x: m.x + 0.5, z: m.z + 0.5, s: Math.max(view.current.s, 1.4) });
  }

  function flyToPlace(p: MinecraftPlace) {
    setSnap("min");
    selectPlace(p);
  }

  function fly(row: ListRow) {
    const [d, x, z] =
      row.kind === "beacon" ? [row.b.dim, row.b.x, row.b.z] : [row.g.dim, ...gateCenter(row.g)];
    select(row.kind === "beacon" ? { kind: "beacon", b: row.b } : { kind: "gate", g: row.g });
    setSnap("min");
    showPlace(d, Math.floor(x), Math.floor(z), 1.25);
  }

  /** "Show Tree farm": both ends of an open gate framed in the visible map; "Show it in the
   *  Nether": its far end, in that dimension. */
  function showGateEnds(g: MinecraftGate) {
    const to = g.target;
    if (!to) return;
    if (to.dim !== g.dim) {
      showPlace(to.dim, to.x, to.z, 1.25);
      return;
    }
    if (g.dim !== state.current.dim) jumpDim(g.dim);
    const [cx, cz] = gateCenter(g);
    const fit = fitView(
      [
        [cx, cz],
        [to.x + 0.5, to.z + 0.5],
      ],
      frame.current,
      72,
      2,
    );
    if (fit) glide(fit);
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
      // A scanner is framed so their whole square shows (not on the replay: none is drawn).
      const scanning = scanShown(hit.p) && !(tl.on && !tl.live);
      const framed = scanning ? scanFrame(hit.p, frame.current) : null;
      if (framed) glide(framed);
      select({ kind: "player", p: hit.p, at: tl.on && !tl.live ? tRef.current : undefined });
      reframe.current = framed ? hit.p.xuid : null;
      return;
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

  // C's ranked list under a villager chip: nearest whoever is on in this dimension.
  const onHere = players?.players.find((q) => q.online && q.dim === dim);
  const rankFrom = onHere
    ? { x: onHere.x, z: onHere.z, who: onHere.gamertag }
    : { x: view.current.x, z: view.current.z, who: null };
  const petOwner = petsOf
    ? players?.players.find(
        (q) => q.dim === dim && q.gamertag.toLowerCase() === petsOf.toLowerCase(),
      )
    : undefined;
  const petFrom = petOwner
    ? { x: petOwner.x, z: petOwner.z, who: petOwner.gamertag }
    : { x: view.current.x, z: view.current.z, who: null };

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
                scan={scan}
                beacons={marks?.beacons ?? []}
                onShowGate={showGateEnds}
                layers={{
                  gates: marks?.gates ?? [],
                  players: players?.players ?? [],
                  roster,
                  world: places?.world ?? "",
                  goals: goalsGot,
                  vcard,
                  ents,
                  filter: vfilter,
                  now: host.nowMs / 1000,
                }}
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
                marks={source.markers ? marks : undefined}
                onFly={fly}
                places={source.places ? places : undefined}
                ents={source.entities ? ents : undefined}
                vfilter={vfilter}
                setVfilter={setVfilter}
                petsOf={petsOf}
                setPetsOf={setPetsOf}
                rankFrom={rankFrom}
                petFrom={petFrom}
                onPlace={flyToPlace}
                onVillager={flyToVillager}
                onPet={flyToPet}
                now={host.nowMs / 1000}
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
  scan,
  beacons,
  onShowGate,
  playerToken: tok,
  layers,
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
  /** The selected player's live scan, in the held Tricorder's words (§M25). */
  scan: ScanWords | null;
  /** Every beacon in the world, for a tapped one's overlaps. */
  beacons: MinecraftBeacon[];
  onShowGate: (g: MinecraftGate) => void;
  playerToken: string;
  /** What this round's cards read (minecraft-map-layers). */
  layers: LayerCtx;
}) {
  let icon: ReactNode;
  let title: string;
  let sub: string;
  let x: number;
  let z: number;
  let y: number | null = null;
  let pill: ReactNode = null;
  const now = nowMs / 1000;
  const where: MinecraftDim =
    sel.kind === "player"
      ? sel.p.dim
      : sel.kind === "event"
        ? (sel.e.dim ?? "overworld")
        : sel.kind === "spawn"
          ? "overworld"
          : sel.kind === "beacon"
            ? sel.b.dim
            : sel.kind === "gate"
              ? sel.g.dim
              : sel.kind === "place"
                ? sel.p.dim
                : sel.dim;
  if (sel.kind === "place") {
    const p = sel.p;
    icon = (
      <span
        className="mc-map-det-ic"
        style={tintOf(tokenFor(layers.roster, p.xuid, p.gamertag))}
        aria-hidden="true"
      >
        <Glyph k="pin" />
      </span>
    );
    title = p.name;
    sub = placeSub(p);
    x = p.x;
    y = p.y;
    z = p.z;
  } else if (sel.kind === "villager") {
    const v = sel.v;
    icon = (
      <span className="mc-map-det-ic" aria-hidden="true">
        <Glyph k={jobOf(v.job).glyph} />
      </span>
    );
    title = villagerTitle(v);
    const size = layers.ents?.villages.find((q) => q.id === v.village)?.count ?? 0;
    sub = size > 1 ? `in the village of${NB}${size}` : "on its own";
    if (v.name) sub = `${v.name} · ${sub}`;
    pill = v.locked_in ? <span className="mc-map-state dorm">job locked in</span> : null;
    x = v.x;
    y = v.y;
    z = v.z;
  } else if (sel.kind === "pet") {
    const m = sel.m;
    const w = mobWords(m);
    icon = (
      <span
        className="mc-map-det-ic"
        style={tintOf(w.whose ? tokenFor(layers.roster, null, w.whose) : null)}
        aria-hidden="true"
      >
        <Glyph k={m.tamed ? "paw" : "tag"} />
      </span>
    );
    title = w.title;
    sub = w.sub;
    x = m.x;
    y = m.y;
    z = m.z;
  } else if (sel.kind === "beacon") {
    const w = beaconWords(sel.b, beacons);
    icon = (
      <span className="mc-map-det-ic bea" aria-hidden="true">
        <ShieldCheckIcon size={18} />
      </span>
    );
    title = w.title;
    sub = w.sub;
    pill = <span className="mc-map-state prot">protecting</span>;
    x = sel.b.x;
    y = sel.b.y;
    z = sel.b.z;
  } else if (sel.kind === "gate") {
    const w = gateWords(sel.g);
    icon = (
      <span className="mc-map-det-ic gate" aria-hidden="true">
        <PortalIcon size={18} />
      </span>
    );
    title = w.title;
    sub = w.sub;
    pill =
      sel.g.state === "open" ? (
        <span className="mc-map-state open">open</span>
      ) : (
        <span className="mc-map-state dorm">dormant</span>
      );
    // The middle of the ring, where a player would stand to step through.
    const [cx, cz] = gateCenter(sel.g);
    x = Math.floor(cx);
    y = sel.g.y;
    z = Math.floor(cz);
  } else if (sel.kind === "spot") {
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
          <div className="mc-map-det-s">
            {pill}
            {pill ? " · " : null}
            {sub}
          </div>
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
      {sel.kind === "beacon" && <BeaconNote b={sel.b} beacons={beacons} />}
      {sel.kind === "place" && <PlaceCard p={sel.p} layers={layers} />}
      {sel.kind === "villager" && <VillagerCard v={sel.v} layers={layers} />}
      {sel.kind === "pet" && <MobCard m={sel.m} dim={sel.dim} layers={layers} />}
      {sel.kind === "gate" && <GateLead g={sel.g} onShow={onShowGate} />}
      {tricorder && !compact && (
        <p className="mc-map-det-tri">
          <CrosshairIcon size={16} />
          <span>
            <b>Tricorder</b> · {tricorder}
          </span>
        </p>
      )}
      {scan && !compact && <ScanLines scan={scan} />}
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

function BeaconNote({ b, beacons }: { b: MinecraftBeacon; beacons: MinecraftBeacon[] }) {
  const w = beaconWords(b, beacons);
  return (
    <p className="mc-note mc-map-det-note">
      <b>48&nbsp;×&nbsp;48 blocks, floor to sky</b> — {w.area} (the 3&nbsp;×&nbsp;3 chunks round it)
      {w.overlaps}. Wild and hostile mobs vanish there; tamed, named, fed and bred ones stay.
    </p>
  );
}

function GateLead({ g, onShow }: { g: MinecraftGate; onShow: (g: MinecraftGate) => void }) {
  const w = gateWords(g);
  if (w.dormant) return <p className="mc-note mc-map-det-note">{w.dormant}</p>;
  return (
    <>
      <p className="mc-map-lead">
        <ArrowRightIcon size={18} />
        <span>
          Leads to <b>{g.target?.place}</b> · {w.lead}
        </span>
      </p>
      {w.show && (
        <div className="mc-btnrow mc-map-det-acts">
          <button type="button" className="mc-btn mc-btn-primary" onClick={() => onShow(g)}>
            {w.show}
          </button>
        </div>
      )}
    </>
  );
}

/** The view that frames a place's 48-block reach with M26's 72 px margin. */
function reachFrame(p: { x: number; z: number }, f: Frame): MapView | null {
  const r = 48;
  return fitView(
    [
      [p.x + 0.5 - r, p.z + 0.5 - r],
      [p.x + 0.5 + r, p.z + 0.5 + r],
    ],
    f,
    72,
    4,
  );
}

/** What this round's cards read (GUI gate minecraft-map-layers). */
interface LayerCtx {
  gates: MinecraftGate[];
  players: MinecraftMapPlayer[];
  roster: RosterEntry[];
  /** The world's folder, to pick its goals out of a player's count. */
  world: string;
  goals: MinecraftGoalProgress | "loading" | "failed" | null;
  vcard: MinecraftVillagerCard | "loading" | "failed" | null;
  ents: MinecraftMapEntities | null;
  filter: VillagerFilter;
  now: number;
}

const NB = "\u00a0";

/** A player's tint and text for a card's icon tile; nobody's keeps the neutral one. */
function tintOf(tok: string | null): React.CSSProperties | undefined {
  if (!tok) return undefined;
  const pair: Record<string, [string, string]> = {
    "--steel": ["--steel-tint", "--steel-text"],
    "--green": ["--green-tint", "--ok-text"],
    "--amber": ["--amber-tint", "--warn-text"],
    "--violet": ["--violet-tint", "--violet-text"],
  };
  const [bg, fg] = pair[tok] ?? ["--steel-tint", "--steel-text"];
  return { background: `var(${bg})`, color: `var(${fg})`, borderColor: "transparent" };
}

/** A Lucide glyph from the map's own set, for the chrome. */
function Glyph({ k, size = 18 }: { k: GlyphKey | "target"; size?: number }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {k === "target" ? (
        <>
          <circle cx="12" cy="12" r="10" />
          <circle cx="12" cy="12" r="6" />
          <circle cx="12" cy="12" r="2" />
        </>
      ) : (
        <path d={GLYPHS[k]} />
      )}
    </svg>
  );
}

function LineIcon({ icon }: { icon: CardLine["icon"] }) {
  if (icon === "shield") return <ShieldIcon size={16} />;
  if (icon === "user") return <UserIcon size={16} />;
  if (icon === "portal") return <PortalIcon size={16} />;
  return <Glyph k="target" size={16} />;
}

function Ln({ line }: { line: CardLine }) {
  return (
    <p className={`mc-map-ln${line.tone ? ` ${line.tone}` : ""}`}>
      <LineIcon icon={line.icon} />
      <span>
        {line.lead && <b>{line.lead}</b>}
        {line.text}
      </span>
    </p>
  );
}

/** A saved place's card (places A): protected or not, where its player is, the gates aimed
 *  at its block, and its share of each counted goal — no Tricorder button (owner). */
function PlaceCard({ p, layers }: { p: MinecraftPlace; layers: LayerCtx }) {
  const player =
    layers.players.find((q) => q.xuid === p.xuid) ??
    layers.players.find((q) => q.gamertag.toLowerCase() === p.gamertag.toLowerCase()) ??
    null;
  const g = goalWords(p, layers.world, layers.goals);
  return (
    <div className="mc-map-lns">
      {placeLines(p, layers.gates, player).map((l) => (
        <Ln key={l.text} line={l} />
      ))}
      <p className="mc-map-ln">
        <Glyph k="target" size={16} />
        <span>
          <b>{g.head}</b> (the circle) · {g.lines.join(" · ")}
        </span>
      </p>
      {g.foot && <p className="mc-note mc-map-det-note">{g.foot}</p>}
    </div>
  );
}

/** A villager's card (villagers A): every trade it offers now, by level, with how much
 *  each has been used — and what isn't told yet. */
function VillagerCard({ v, layers }: { v: MinecraftVillager; layers: LayerCtx }) {
  const near = layers.players
    .filter((q) => q.online && q.dim === layers.ents?.dimension)
    .map((q) => ({ q, d: Math.hypot(q.x - v.x, q.z - v.z) }))
    .sort((a, b) => a.d - b.d)[0]?.q;
  const c = layers.vcard;
  const age = entitiesAge(layers.ents, layers.now);
  const card = c && c !== "loading" && c !== "failed" ? c.found : null;
  const tiers = tradeTiers(card, layers.filter);
  return (
    <div className="mc-map-lns">
      {near && (
        <Ln
          line={{
            tone: "",
            icon: "user",
            text: (() => {
              const { way, blocks } = bearingTo(near.x, near.z, v.x + 0.5, v.z + 0.5);
              return `${blocks}${NB}blocks ${way} of ${near.gamertag}`;
            })(),
          }}
        />
      )}
      {c === "loading" && <p className="mc-note mc-map-det-note">Reading its trades…</p>}
      {c === "failed" && (
        <p className="mc-note mc-map-det-note">Can&apos;t read its trades right now.</p>
      )}
      {c && c !== "loading" && c !== "failed" && !card && (
        <p className="mc-note mc-map-det-note">
          The saved world doesn&apos;t have this villager any more: it may have died, or the world
          has been saved since.
        </p>
      )}
      {tiers.length > 0 && (
        <div className="mc-map-trades">
          {tiers.map((tier) => (
            <div key={tier.level} className="mc-map-tier">
              <div className="mc-map-tier-h">{tier.level}</div>
              {tier.trades.map((tr) => (
                <div
                  key={tr.key}
                  className={`mc-map-tr${tr.gone ? " gone" : ""}${tr.hit ? " hit" : ""}`}
                >
                  <span className="mc-map-tr-l">
                    {tr.what}
                    {tr.enchant && (
                      <>
                        {" · "}
                        <b>{tr.enchant}</b>
                      </>
                    )}
                    <small>{tr.price}</small>
                  </span>
                  <span className="mc-map-tr-u">{tr.uses}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
      {card && <p className="mc-note mc-map-det-note">{tradesNote(card, age)}</p>}
    </div>
  );
}

/** A pet's or named mob's card: whose (or that the owner isn't known yet), where it was
 *  at the last save, and that it may have moved. */
function MobCard({ m, dim, layers }: { m: MinecraftMob; dim: MinecraftDim; layers: LayerCtx }) {
  const owner = m.owner_name
    ? (layers.players.find((q) => q.gamertag.toLowerCase() === m.owner_name?.toLowerCase()) ?? null)
    : null;
  const from = mobFrom(m, owner, dim);
  return (
    <div className="mc-map-lns">
      {from && <Ln line={{ tone: "", icon: "user", text: from }} />}
      <p className="mc-note mc-map-det-note">{petNote(m, entitiesAge(layers.ents, layers.now))}</p>
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
  marks,
  onFly,
  places,
  ents,
  vfilter,
  setVfilter,
  petsOf,
  setPetsOf,
  rankFrom,
  petFrom,
  onPlace,
  onVillager,
  onPet,
  now,
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
  /** The world's beacons and gates; undefined where the map has none to show (a share). */
  marks: MinecraftMapMarkers | null | undefined;
  onFly: (row: ListRow) => void;
  /** Saved places, and the villagers and pets: undefined where the map has none to show
   *  (a share link — owner-only). */
  places: MinecraftMapPlaces | null | undefined;
  ents: MinecraftMapEntities | null | undefined;
  vfilter: VillagerFilter;
  setVfilter: (f: VillagerFilter) => void;
  petsOf: string | null;
  setPetsOf: (x: string | null) => void;
  rankFrom: { x: number; z: number; who: string | null };
  petFrom: { x: number; z: number; who: string | null };
  onPlace: (p: MinecraftPlace) => void;
  onVillager: (v: MinecraftVillager) => void;
  onPet: (m: MinecraftMob) => void;
  now: number;
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
  const hasMarks = marks !== undefined;
  const lines = overlayLines(marks ?? null);
  const rows = listRows(marks ?? null);
  const hasPlaces = places !== undefined;
  const hasEnts = ents !== undefined;
  const groups = placeGroups(places ?? null, roster);
  const ranked = ov.villagers ? rankedVillagers(ents ?? null, vfilter, rankFrom) : [];
  const rankedP = ov.pets ? rankedPets(ents ?? null, petsOf, petFrom) : [];
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
        {hasMarks && (
          <>
            <OverlayRow
              icon={<ShieldCheckIcon size={18} />}
              tone="bea"
              title="No-Spawn Beacons"
              sub={lines.beacons}
              on={ov.beacons}
              onToggle={() => toggle("beacons")}
            />
            <OverlayRow
              icon={<PortalIcon size={18} />}
              tone="gate"
              title="Wormhole gates"
              sub={lines.gates}
              on={ov.gates}
              onToggle={() => toggle("gates")}
            />
          </>
        )}
        {hasPlaces && (
          <OverlayRow
            icon={<Glyph k="pin" />}
            title="Saved places"
            sub={placesLine(places ?? null)}
            on={ov.places}
            onToggle={() => toggle("places")}
          />
        )}
        {hasEnts && (
          <>
            <OverlayRow
              icon={<Glyph k="house" />}
              title="Villagers"
              sub={villagersLine(ents ?? null, now)}
              on={ov.villagers}
              onToggle={() => toggle("villagers")}
            />
            {ov.villagers && (
              <div className="mc-chips mc-map-who" role="radiogroup" aria-label="Which villagers">
                {villagerChips(ents ?? null).map((c) => (
                  <button
                    key={filterKey(c.f)}
                    type="button"
                    className="mc-chip"
                    // biome-ignore lint/a11y/useSemanticElements: a styled chip; a native radio can't carry this layout.
                    role="radio"
                    aria-checked={filterKey(vfilter) === filterKey(c.f)}
                    onClick={() => setVfilter(c.f)}
                  >
                    {c.label}
                  </button>
                ))}
              </div>
            )}
            {ranked.length > 0 && (
              <div className="mc-map-list mc-map-ranked" aria-label="Nearest first">
                {ranked.map((r, i) => (
                  <button
                    key={r.key}
                    type="button"
                    className="mc-map-li"
                    onClick={() => onVillager(r.v)}
                  >
                    <span className="mc-map-det-ic mc-map-rank" aria-hidden="true">
                      {i + 1}
                    </span>
                    <span className="mc-map-li-tx">
                      <b>{r.title}</b>
                      <span>{r.sub}</span>
                    </span>
                    <ChevronRightIcon size={18} />
                  </button>
                ))}
              </div>
            )}
            <OverlayRow
              icon={<Glyph k="paw" />}
              title="Pets & named mobs"
              sub={petsLine(ents ?? null, now)}
              on={ov.pets}
              onToggle={() => toggle("pets")}
            />
            {ov.pets && petChips(ents ?? null).length > 0 && (
              <div className="mc-chips mc-map-who" role="radiogroup" aria-label="Whose pets">
                {[null, ...petChips(ents ?? null)].map((who) => {
                  const tok = who ? tokenFor(roster, null, who) : null;
                  return (
                    <button
                      key={who ?? "everyone"}
                      type="button"
                      className="mc-chip"
                      // biome-ignore lint/a11y/useSemanticElements: a styled chip; a native radio can't carry this layout.
                      role="radio"
                      aria-checked={petsOf === who}
                      onClick={() => setPetsOf(who)}
                    >
                      {tok && (
                        <span
                          className="pd"
                          style={{ background: `var(${tok})` }}
                          aria-hidden="true"
                        />
                      )}
                      {who ? `${who}'s` : "Everyone's"}
                    </button>
                  );
                })}
              </div>
            )}
            {rankedP.length > 0 && (
              <div className="mc-map-list mc-map-ranked" aria-label="Their pets, nearest first">
                {rankedP.map((r, i) => (
                  <button
                    key={r.key}
                    type="button"
                    className="mc-map-li"
                    onClick={() => onPet(r.m)}
                  >
                    <span className="mc-map-det-ic mc-map-rank" aria-hidden="true">
                      {i + 1}
                    </span>
                    <span className="mc-map-li-tx">
                      <b>{r.title}</b>
                      <span>{r.sub}</span>
                    </span>
                    <ChevronRightIcon size={18} />
                  </button>
                ))}
              </div>
            )}
          </>
        )}
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
      </div>

      {(rows.length > 0 || groups.length > 0) && (
        <>
          <h3 className="mc-sect mc-map-sect">In this world</h3>
          {groups.map((g) => (
            <div key={g.gamertag}>
              <div className="mc-map-grp">
                <span
                  className="pd"
                  style={{ background: g.token ? `var(${g.token})` : "var(--text-2)" }}
                  aria-hidden="true"
                />
                {g.gamertag}&apos;s places
              </div>
              <div className="mc-map-list">
                {g.rows.map((r) => (
                  <button
                    key={r.key}
                    type="button"
                    className="mc-map-li"
                    onClick={() => onPlace(r.p)}
                  >
                    <span className="mc-map-det-ic" style={tintOf(g.token)} aria-hidden="true">
                      <Glyph k="pin" size={16} />
                    </span>
                    <span className="mc-map-li-tx">
                      <b>{r.title}</b>
                      <span>{r.sub}</span>
                    </span>
                    <ChevronRightIcon size={18} />
                  </button>
                ))}
              </div>
            </div>
          ))}
          <div className="mc-map-list">
            {rows.map((r) => (
              <button key={r.key} type="button" className="mc-map-li" onClick={() => onFly(r)}>
                <span
                  className={`mc-map-det-ic ${r.kind === "beacon" ? "bea" : "gate"}`}
                  aria-hidden="true"
                >
                  {r.kind === "beacon" ? <ShieldCheckIcon size={16} /> : <PortalIcon size={16} />}
                </span>
                <span className="mc-map-li-tx">
                  <b>{r.title}</b>
                  <span>{r.sub}</span>
                </span>
                <ChevronRightIcon size={18} />
              </button>
            ))}
          </div>
        </>
      )}

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
        {hasMarks && (
          <>
            <div className="mc-map-lg-row">
              <span className="mc-map-lg-i">
                <MarkSwatch kind="beacon" />
                No-Spawn Beacon
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="area" />
                Protected (48 × 48)
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="open" />
                Gate · open
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="dormant" />
                Gate · dormant
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="link" />
                Where it leads
              </span>
            </div>
            <p className="mc-note">
              Positions are the pack&apos;s own records, sent when a beacon or gate changes and
              again at every start.
            </p>
          </>
        )}
        {hasPlaces && (
          <>
            <div className="mc-map-lg-row">
              <span className="mc-map-lg-i">
                <MarkSwatch kind="place" />
                Saved place (ring: whose)
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="reach" />
                Where goals count chests (tapped)
              </span>
            </div>
            <p className="mc-note">
              Places are each player&apos;s own, saved through Dave where they stood. The circle is
              the 48&nbsp;blocks Dave counts a goal&apos;s chests in.
            </p>
          </>
        )}
        {hasEnts && (ov.villagers || ov.pets) && (
          <>
            <div className="mc-map-lg-row">
              <span className="mc-map-lg-i">
                <MarkSwatch kind="village" />
                Village (count)
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="villager" />
                Villager, by job
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="pet" />
                Pet (ring: whose)
              </span>
              <span className="mc-map-lg-i">
                <MarkSwatch kind="named" />
                Named mob
              </span>
            </div>
            <div className="mc-map-lg-row">
              {(
                [
                  ["book", "Librarian"],
                  ["flask", "Cleric"],
                  ["wheat", "Farmer"],
                  ["hammer", "Smiths"],
                  ["fish", "Fisherman"],
                  ["mapFold", "Cartographer"],
                  ["user", "Others"],
                ] as [GlyphKey, string][]
              ).map(([k, name]) => (
                <span className="mc-map-lg-i" key={k}>
                  <Glyph k={k} size={14} />
                  {name}
                </span>
              ))}
            </div>
            <p className="mc-note">
              From the saved world, {entitiesAge(ents ?? null, now)}: villagers and pets move, so a
              live world can differ. Only the trades a villager offers now are told; wandering
              traders are visitors and aren&apos;t shown.
            </p>
          </>
        )}
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

/** The legend's beacon and gate swatches: the map's own glyphs on the neutral void, drawn
 *  as SVG with the same tokens the canvas reads. */
function MarkSwatch({
  kind,
}: {
  kind:
    | "beacon"
    | "area"
    | "open"
    | "dormant"
    | "link"
    | "place"
    | "reach"
    | "village"
    | "villager"
    | "pet"
    | "named";
}) {
  const tile = (ring: string, dashed: boolean) => (
    <>
      <rect x="4" y="4" width="16" height="16" rx="5" fill="var(--map-halo)" />
      <rect
        x="5"
        y="5"
        width="14"
        height="14"
        rx="4"
        fill="none"
        stroke={ring}
        strokeWidth="1.2"
        strokeDasharray={dashed ? "2.4 2" : undefined}
      />
    </>
  );
  return (
    <svg className="mc-map-lg-sw" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
      <rect x="0" y="0" width="24" height="24" rx="5" fill="var(--map-void)" />
      {kind === "beacon" && (
        <>
          {tile("var(--green)", false)}
          <path
            d={GLYPHS.shield}
            transform="translate(7 7) scale(0.42)"
            fill="none"
            stroke="var(--map-ink)"
            strokeWidth="2.4"
          />
        </>
      )}
      {kind === "area" && (
        <rect
          x="4"
          y="4"
          width="16"
          height="16"
          fill="color-mix(in srgb, var(--green) 24%, transparent)"
          stroke="var(--green)"
          strokeWidth="2"
          strokeDasharray="4 2.5"
        />
      )}
      {(kind === "open" || kind === "dormant") && (
        <>
          {tile("var(--violet)", kind === "dormant")}
          <g transform="translate(7 7) scale(0.42)" stroke="var(--map-ink)" strokeWidth="2.4">
            <path d={GLYPHS.portalFrame} fill={kind === "open" ? "var(--violet)" : "none"} />
            <path d={GLYPHS.swirl} fill="none" />
          </g>
        </>
      )}
      {(kind === "place" || kind === "villager") && (
        <>
          <circle cx="12" cy="12" r="8" fill="var(--map-halo)" />
          <circle
            cx="12"
            cy="12"
            r="7"
            fill="none"
            stroke={kind === "place" ? "var(--steel)" : "var(--map-ink)"}
            strokeOpacity={kind === "place" ? 1 : 0.55}
            strokeWidth="1.2"
          />
          <path
            d={kind === "place" ? GLYPHS.pin : GLYPHS.book}
            transform="translate(7 7) scale(0.42)"
            fill="none"
            stroke="var(--map-ink)"
            strokeWidth="2.4"
          />
        </>
      )}
      {kind === "reach" && (
        <circle
          cx="12"
          cy="12"
          r="8"
          fill="color-mix(in srgb, var(--steel) 10%, transparent)"
          stroke="var(--steel)"
          strokeWidth="1.2"
        />
      )}
      {(kind === "village" || kind === "pet" || kind === "named") && (
        <>
          <rect x="4" y="4" width="16" height="16" rx="5" fill="var(--map-halo)" />
          <rect
            x="5"
            y="5"
            width="14"
            height="14"
            rx="4"
            fill="none"
            stroke={
              kind === "village"
                ? "var(--amber)"
                : kind === "pet"
                  ? "var(--steel)"
                  : "var(--map-ink)"
            }
            strokeOpacity={kind === "named" ? 0.55 : 1}
            strokeWidth="1.2"
          />
          <path
            d={GLYPHS[kind === "village" ? "house" : kind === "pet" ? "paw" : "tag"]}
            transform="translate(7 7) scale(0.42)"
            fill="none"
            stroke="var(--map-ink)"
            strokeWidth="2.4"
          />
        </>
      )}
      {kind === "link" && (
        <path
          d="M5 19 19 5"
          stroke="var(--violet)"
          strokeWidth="3"
          strokeLinecap="round"
          strokeDasharray="0.1 4.5"
        />
      )}
    </svg>
  );
}

function OverlayRow({
  icon,
  title,
  sub,
  on,
  onToggle,
  children,
  tone,
}: {
  icon: ReactNode;
  title: string;
  sub: string;
  on: boolean;
  onToggle: () => void;
  children?: ReactNode;
  /** The glyph's accent: a beacon's green or a gate's violet. */
  tone?: "bea" | "gate" | undefined;
}) {
  return (
    <div className="mc-map-ov">
      <span className={`mc-map-ov-gl${tone ? ` ${tone}` : ""}`} aria-hidden="true">
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
