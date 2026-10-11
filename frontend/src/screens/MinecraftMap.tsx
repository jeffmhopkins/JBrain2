// The Minecraft map — a full-bleed page pushed from the Minecraft screen (binding mock
// docs/mocks/minecraft-map/a-full-bleed.html, variant A; plan §M8). The map fills the phone;
// one persistent bottom panel holds the world, the status line, what you tapped and the
// layers. It is not a <Sheet>: it co-occupies the screen like the graph Map's panel
// (DESIGN "Minecraft map"). Wave 1 is the live view; the timeline (M8a) comes later.
//
// The picture is the box's 256-px biome tiles; everything over it (players, structures,
// the tapped spot, labels, the scale bar) is drawn on one canvas so markers can cluster and
// labels can dodge each other and the floating controls.

import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  type TouchEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  ApiError,
  MOCK_MODE,
  type MinecraftDim,
  type MinecraftMapInfo,
  type MinecraftMapPlayer,
  type MinecraftMapPlayers,
  type MinecraftMapPoint,
  type MinecraftMapStructures,
  type MinecraftSlot,
  type MinecraftStructure,
  api,
  minecraftTileUrl,
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
  GlobeIcon,
  GridIcon,
  InfoIcon,
  LayersIcon,
  LocateIcon,
  MapIcon,
  MinusIcon,
  PinIcon,
  PlusIcon,
  PortalIcon,
  RouteIcon,
  SatelliteIcon,
  SkullIcon,
  UserIcon,
  XIcon,
} from "../components/icons";
import { agoOf } from "../minecraft";
import {
  type Cluster,
  DIMS,
  type Frame,
  GLYPHS,
  type MapView,
  PIN_SCALE,
  type Placed,
  clusterMarkers,
  clusterTarget,
  dimName,
  entryLine,
  fitView,
  hitTest,
  initialOf,
  overworldSide,
  panBy,
  playersIn,
  scaleBar,
  slotLabel,
  slotLine,
  spotText,
  statusLine,
  structureInfo,
  tileKey,
  toScreen,
  toWorld,
  visibleTiles,
  zoomAt,
} from "../minecraftMap";
import { useForeground } from "../visibility";
import { useCovered, useW } from "./MinecraftWorlds";

/** Online players' positions arrive through the travel log every ~10 s. */
const PLAYERS_POLL_MS = 10_000;
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
  | { kind: "player"; p: MinecraftMapPlayer }
  | { kind: "struct"; s: MinecraftStructure; dim: MinecraftDim };

type Hit =
  | { kind: "player"; sx: number; sy: number; p: MinecraftMapPlayer }
  | { kind: "struct"; sx: number; sy: number; s: MinecraftStructure }
  | { kind: "cluster"; sx: number; sy: number; members: StructMarker[] };

interface StructMarker extends Placed {
  s: MinecraftStructure;
}

/** Player colours: the four accents the mock gives its players, in roster order. */
const PLAYER_TOKENS = ["--steel", "--green", "--amber", "--violet"];

type TileEntry = { img: CanvasImageSource | null; at: number; state: "ok" | "loading" | "failed" };

function loadImage(url: string): Promise<CanvasImageSource> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("tile"));
    img.src = url;
  });
}

async function fetchTile(
  slot: string,
  dim: MinecraftDim,
  zoom: number,
  tx: number,
  tz: number,
): Promise<CanvasImageSource> {
  if (MOCK_MODE) {
    const m = await import("../minecraftMapFixtures");
    return m.mockTile(slot, dim, zoom, tx, tz);
  }
  return loadImage(minecraftTileUrl(slot, dim, zoom, tx, tz));
}

interface Tokens {
  voidc: string;
  halo: string;
  plate: string;
  ink: string;
  inkOn: string;
  text: string;
  amber: string;
  border: string;
  s2: string;
  players: string[];
}

function readTokens(el: Element): Tokens {
  const cs = getComputedStyle(el);
  const g = (n: string) => cs.getPropertyValue(n).trim();
  return {
    voidc: g("--map-void"),
    halo: g("--map-halo"),
    plate: g("--map-plate"),
    ink: g("--map-ink"),
    inkOn: g("--map-ink-on"),
    text: g("--text"),
    amber: g("--amber"),
    border: g("--border"),
    s2: g("--surface-2"),
    players: PLAYER_TOKENS.map(g),
  };
}

const reducedMotion = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

const PATHS = new Map<string, Path2D>();
function glyphPath(key: keyof typeof GLYPHS): Path2D | null {
  if (typeof Path2D === "undefined") return null;
  let p = PATHS.get(key);
  if (!p) {
    p = new Path2D(GLYPHS[key]);
    PATHS.set(key, p);
  }
  return p;
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : "Can't reach the box.";
}

/** An unreachable box (or sidecar) rather than a refusal about this world. */
const isUnreachable = (err: unknown) =>
  !(err instanceof ApiError) || err.status >= 502 || err.status === 0;

export function MapLayer({ slot: startSlot }: { slot?: string | undefined }) {
  const w = useW();
  const pop = w.pop;
  useBackLayer(pop);
  const root = useRef<HTMLDivElement>(null);
  const covered = useCovered(w, root);
  const back = useRef<HTMLButtonElement>(null);
  const mapEl = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const dragEl = useRef<HTMLDivElement>(null);
  const selEl = useRef<HTMLDivElement>(null);
  const gridEl = useRef<HTMLDivElement>(null);
  const foreground = useForeground();

  const [slot, setSlot] = useState(startSlot ?? w.active?.id ?? "slot1");
  const [dim, setDim] = useState<MinecraftDim>("overworld");
  const [snap, setSnap] = useState<Snap>("min");
  const [sel, setSel] = useState<Sel | null>(null);
  const [point, setPoint] = useState<MinecraftMapPoint | null>(null);
  const [ov, setOv] = useState({ players: true, structures: true, grid: false });
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

  const slotRec: MinecraftSlot | null = w.slotOf(slot);
  const loaded = !!slotRec?.active;
  const live = loaded && (players?.live ?? w.running);
  const onlineCount = players ? players.players.filter((p) => p.online).length : w.online.length;

  // Everything the draw loop reads, current as of the last render.
  const view = useRef<MapView>({ x: 0, z: 0, s: 0.7 });
  const frame = useRef<Frame>({ w: 1, h: 1, bottom: 160, top: 64 });
  const dirty = useRef(true);
  const hits = useRef<Hit[]>([]);
  const cache = useRef(new Map<string, TileEntry>());
  const inFlight = useRef(0);
  const needsFit = useRef(true);
  const tk = useRef<Tokens | null>(null);
  const live$ = useRef(live);
  live$.current = live;
  const state = useRef({ slot, dim, sel, ov, players, structs, info });
  state.current = { slot, dim, sel, ov, players, structs, info };

  const redraw = useCallback(() => {
    dirty.current = true;
  }, []);
  // biome-ignore lint/correctness/useExhaustiveDependencies: any change to what's drawn redraws.
  useEffect(redraw, [slot, dim, sel, ov, players, structs, info, panelH]);

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
    api
      .minecraftMapInfo(slot, dim)
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
  }, [slot, dim]);

  useEffect(() => {
    if (!foreground) return;
    let gone = false;
    const read = () =>
      api
        .minecraftMapPlayers(slot)
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
  }, [slot, foreground]);

  const sweeping = structs?.sweep.state === "running";
  useEffect(() => {
    let gone = false;
    const read = () =>
      api
        .minecraftMapStructures(slot, dim)
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
  }, [slot, dim, sweeping, foreground]);

  // What a tapped spot or structure stands on.
  useEffect(() => {
    if (!sel || sel.kind === "player") {
      setPoint(null);
      return;
    }
    const [x, z] = sel.kind === "spot" ? [sel.x, sel.z] : [sel.s.x, sel.s.z];
    let gone = false;
    setPoint(null);
    api
      .minecraftMapPoint(slot, sel.dim, x, z)
      .then((p) => {
        if (!gone) setPoint(p);
      })
      .catch(() => {
        if (!gone) setPoint({ x, z, dimension: sel.dim, source: "unknown" });
      });
    return () => {
      gone = true;
    };
  }, [sel, slot]);

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
        const f = Math.min(1, (now - t0) / 200);
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
      const pts: [number, number][] = (on.length ? on : here).map((p) => [p.x, p.z]);
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

  // Fit once per world and dimension, when what to fit has arrived.
  useEffect(() => {
    if (!needsFit.current || !info || (!players && !playersFailed)) return;
    needsFit.current = false;
    fitEveryone(false);
  }, [info, players, playersFailed, fitEveryone]);

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
    needsFit.current = true;
  }

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
    (key: string, t: { zoom: number; tx: number; tz: number }): TileEntry | null => {
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
      fetchTile(st.slot, st.dim, t.zoom, t.tx, t.tz)
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
    [redraw],
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
    let pending = 0;
    for (const s of spots) {
      const entry = want(tileKey(st.slot, st.dim, s), s);
      if (entry?.img)
        ctx.drawImage(entry.img, 0, 0, 256, 256, s.sx, s.sy, s.size + 0.5, s.size + 0.5);
      else {
        if (entry?.state !== "failed") pending++;
        hole(ctx, s.sx, s.sy, s.size, entry?.state === "failed" ? "failed" : "loading", t);
      }
    }
    setTiles((p) =>
      p.visible === spots.length && p.pending === pending ? p : { visible: spots.length, pending },
    );

    const labels: Label[] = [];
    const nextHits: Hit[] = [];
    if (st.ov.grid) grid(ctx, v, f, t, labels);

    // structures, clustered
    const sel = st.sel;
    const selStruct = sel?.kind === "struct" ? sel.s : null;
    if (st.ov.structures && st.structs && st.structs.dimension === st.dim) {
      const placed: StructMarker[] = [];
      for (const s of st.structs.structures) {
        const [sx, sy] = toScreen(v, f, s.x + 0.5, s.z + 0.5);
        if (sx < -30 || sy < -30 || sx > f.w + 30 || sy > f.h + 30) continue;
        if (
          selStruct &&
          selStruct.x === s.x &&
          selStruct.z === s.z &&
          selStruct.structure === s.structure
        )
          continue;
        placed.push({ x: s.x, z: s.z, sx, sy, pr: 5, s });
      }
      for (const cl of clusterMarkers(placed)) {
        const one = cl.members[0];
        if (cl.members.length === 1 && one) {
          structGlyph(ctx, one.sx, one.sy, one.s.structure, t, false);
          if (v.s >= 0.25)
            labels.push({
              txt: structureInfo(one.s.structure).label,
              x: one.sx,
              y: one.sy - 8,
              pr: 5,
            });
          nextHits.push({ kind: "struct", sx: one.sx, sy: one.sy, s: one.s });
        } else {
          clusterBadge(ctx, cl, t);
          nextHits.push({ kind: "cluster", sx: cl.sx, sy: cl.sy, members: cl.members });
        }
      }
    }

    // players: never clustered; online ones pulse
    let animate = false;
    if (st.ov.players && st.players) {
      const roster = [...st.players.players].sort((a, b) => a.gamertag.localeCompare(b.gamertag));
      for (const p of playersIn(st.players.players, st.dim)) {
        const [sx, sy] = toScreen(v, f, p.x, p.z);
        if (sx < -30 || sy < -30 || sx > f.w + 30 || sy > f.h + 30) continue;
        const col = t.players[roster.indexOf(p) % t.players.length] ?? t.ink;
        const on = sel?.kind === "player" && sel.p.xuid === p.xuid;
        if (p.online && !reducedMotion() && document.visibilityState === "visible") animate = true;
        playerDisc(ctx, sx, sy, p, col, t, on);
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

    // the tapped structure, on top with its ring and label
    if (selStruct && sel?.kind === "struct" && sel.dim === st.dim) {
      const [sx, sy] = toScreen(v, f, selStruct.x + 0.5, selStruct.z + 0.5);
      structGlyph(ctx, sx, sy, selStruct.structure, t, true);
      labels.push({ txt: structureInfo(selStruct.structure).label, x: sx, y: sy - 8, pr: -1 });
      nextHits.push({ kind: "struct", sx, sy, s: selStruct });
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
    if (snap === "mid") {
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

  function tap(sx: number, sy: number) {
    const hit = hitTest(hits.current, sx, sy);
    if (hit?.kind === "cluster") {
      const to = clusterTarget(hit.members, view.current.s);
      const first = hit.members[0];
      if (to) glide(to);
      else if (first) select({ kind: "struct", s: first.s, dim });
      return;
    }
    if (hit?.kind === "player") return select({ kind: "player", p: hit.p });
    if (hit?.kind === "struct") return select({ kind: "struct", s: hit.s, dim });
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
      const sweep = await api.minecraftMapSweep(slot);
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

  const status = statusLine({
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
        <button type="button" className="mc-map-fab" onClick={pop} aria-label="Back" ref={back}>
          <ChevronLeftIcon size={20} />
        </button>
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
            <button
              type="button"
              className="mc-map-world"
              aria-label={`World: ${title} — change`}
              onClick={() => setPicker(true)}
            >
              {title}
              <ChevronDownIcon size={16} />
            </button>
            <div className="mc-seg mc-map-tabs" role="tablist" aria-label="Panel">
              <button type="button" role="tab" aria-selected="true" aria-checked="true">
                <LayersIcon size={16} />
                Layers
              </button>
              <button
                type="button"
                role="tab"
                aria-selected="false"
                disabled
                title="The timeline comes with the next wave"
              >
                <ClockIcon size={16} />
                Timeline
              </button>
            </div>
          </div>
          <div className="mc-map-stat">
            <output className="mc-map-status">
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
                nowMs={w.nowMs}
                onClose={() => select(null)}
                onCopy={(x, z) => void copy(x, z)}
                onNudge={nudge}
                playerToken={playerToken(sel, players)}
              />
            </div>
          )}
        </div>
        <div className="mc-map-body">
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
                <b>Can&apos;t reach the box.</b> Pan and zoom still work over the tiles this phone
                already has; the rest fills in when it&apos;s back.
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
            structs={structs}
            canSearch={live && !sweeping}
            onSearch={() => void searchAgain()}
            dim={dim}
          />
        </div>
      </div>

      {picker && (
        <Sheet title="Map of…" onClose={() => setPicker(false)}>
          <p className="mc-note mc-map-picker-note">
            Any world slot. Only the loaded one shows live positions.
          </p>
          <div className="mc-map-worlds">
            {w.slots.map((s) => (
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
                    Slot {s.id.replace(/\D/g, "")} · {slotLine(s, w.running)}
                  </span>
                </span>
                {s.active && <span className="badge ok mc-loaded">loaded</span>}
              </button>
            ))}
          </div>
        </Sheet>
      )}
      {toast && (
        <output className="toast mc-toast mc-map-toast" style={{ bottom: panelH + 12 }}>
          {toast}
        </output>
      )}
    </div>
  );
}

function playerToken(sel: Sel, players: MinecraftMapPlayers | null): string {
  if (sel.kind !== "player" || !players) return PLAYER_TOKENS[0] ?? "--steel";
  const roster = [...players.players].sort((a, b) => a.gamertag.localeCompare(b.gamertag));
  const i = roster.findIndex((p) => p.xuid === sel.p.xuid);
  return PLAYER_TOKENS[Math.max(0, i) % PLAYER_TOKENS.length] ?? "--steel";
}

// ---- what you tapped ----

function Detail({
  sel,
  point,
  nowMs,
  onClose,
  onCopy,
  onNudge,
  playerToken: tok,
}: {
  sel: Sel;
  point: MinecraftMapPoint | null;
  nowMs: number;
  onClose: () => void;
  onCopy: (x: number, z: number) => void;
  onNudge: (dx: number, dz: number) => void;
  playerToken: string;
}) {
  let icon: ReactNode;
  let title: string;
  let sub: string;
  let x: number;
  let z: number;
  let y: number | null = null;
  const where = sel.kind === "player" ? sel.p.dim : sel.dim;
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
  } else {
    const p = sel.p;
    icon = (
      <span className="mc-map-det-ic pl" style={{ background: `var(${tok})` }} aria-hidden="true">
        {initialOf(p.gamertag)}
      </span>
    );
    title = p.gamertag;
    sub = p.online
      ? `online · here now · ${dimName(p.dim)}`
      : `offline · logged off here ${agoOf(p.at, nowMs)}`;
    x = Math.round(p.x);
    z = Math.round(p.z);
    y = Math.round(p.y);
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
            X{" "}
            {ox} · Z{" "}
            {oz}
          </b>
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

/** The renderer's biome colours, for the legend: data from inside the picture. */
const LEGEND: Record<MinecraftDim, [string, [number, number, number]][]> = {
  overworld: [
    ["Plains", [141, 179, 96]],
    ["Forest", [5, 102, 33]],
    ["Birch Forest", [48, 116, 68]],
    ["Dark Forest", [64, 81, 26]],
    ["Taiga", [11, 102, 89]],
    ["Snowy Plains", [255, 255, 255]],
    ["Desert", [250, 148, 24]],
    ["Savanna", [189, 178, 95]],
    ["Jungle", [83, 123, 9]],
    ["Swamp", [7, 249, 178]],
    ["Windswept Hills", [96, 96, 96]],
    ["Stony Peaks", [123, 143, 116]],
    ["Snowy Slopes", [196, 196, 196]],
    ["Cherry Grove", [255, 145, 200]],
    ["Meadow", [131, 187, 109]],
    ["Beach", [250, 222, 85]],
    ["River", [0, 0, 255]],
    ["Ocean", [0, 0, 112]],
    ["Deep Ocean", [0, 0, 48]],
  ],
  nether: [
    ["Nether Wastes", [191, 59, 59]],
    ["Crimson Forest", [221, 8, 8]],
    ["Warped Forest", [73, 144, 123]],
    ["Soul Sand Valley", [94, 56, 48]],
    ["Basalt Deltas", [64, 54, 54]],
  ],
  the_end: [["The End", [128, 128, 255]]],
};

const asSurvey = (c: [number, number, number]) => {
  const g = c[0] * 0.3 + c[1] * 0.59 + c[2] * 0.11;
  return c.map((v) => Math.round((g + (v - g) * 0.5) * 0.65)).join(",");
};

function Layers({
  gridRef,
  ov,
  setOv,
  structs,
  canSearch,
  onSearch,
  dim,
}: {
  gridRef: React.RefObject<HTMLDivElement>;
  ov: { players: boolean; structures: boolean; grid: boolean };
  setOv: (f: (o: { players: boolean; structures: boolean; grid: boolean }) => typeof o) => void;
  structs: MinecraftMapStructures | null;
  canSearch: boolean;
  onSearch: () => void;
  dim: MinecraftDim;
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
        {/* biome-ignore lint/a11y/useSemanticElements: a styled option card; a native radio can't carry this layout. */}
        <button type="button" className="mc-map-ly" role="radio" aria-checked="true">
          <span
            className="mc-map-sw"
            style={{
              background: `linear-gradient(135deg,rgb(${forest.join(",")}) 0 50%,rgb(${asSurvey(plains)}) 50%)`,
            }}
            aria-hidden="true"
          />
          <span className="mc-map-ly-tx">
            <span className="mc-map-ly-t">Biomes</span>
            <span className="mc-map-ly-s">explored + satellite survey</span>
          </span>
        </button>
        {[
          ["Surface", "true colour"],
          ["Height", "contours"],
        ].map(([t, s]) => (
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
          icon={<GridIcon size={18} />}
          title="Grid & coordinates"
          sub=""
          on={ov.grid}
          onToggle={() => toggle("grid")}
        />
        {(
          [
            [<RouteIcon key="r" size={18} />, "Trails", "with the timeline"],
            [<SkullIcon key="s" size={18} />, "Deaths", "with the timeline"],
            [<BedIcon key="b" size={18} />, "Beds & spawn", "with the timeline"],
            [<PinIcon key="p" size={18} />, "Saved places", "with Minecraft_Dave's places"],
            [<EyeIcon key="e" size={18} />, "Explored by…", "the travel log’s fog of war"],
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
          Biome colours, hillshaded from the north-west. Water darkens with depth.
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
            <span className="mc-map-lg-n" aria-hidden="true">
              3
            </span>
            Grouped — tap to zoom in
          </span>
        </div>
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

interface Label {
  txt: string;
  x: number;
  y: number;
  pr: number;
  left?: boolean;
}

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const FONT = "500 12px system-ui, sans-serif";

function stars(ctx: CanvasRenderingContext2D, f: Frame, t: Tokens) {
  ctx.save();
  ctx.fillStyle = t.ink;
  let seed = 17;
  const r = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  for (let i = 0; i < 140; i++) {
    const x = r() * f.w;
    const y = r() * f.h;
    ctx.globalAlpha = 0.12 + r() * 0.3;
    const big = r() < 0.15 ? 2 : 1;
    ctx.fillRect(Math.round(x), Math.round(y), big, big);
  }
  ctx.restore();
}

/** A tile not here yet: shimmer-hatched while it loads, finely hatched if it failed. */
function hole(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  s: number,
  st: "loading" | "failed",
  t: Tokens,
) {
  ctx.save();
  ctx.fillStyle = t.s2;
  ctx.fillRect(x, y, s, s);
  ctx.beginPath();
  ctx.rect(x, y, s, s);
  ctx.clip();
  ctx.strokeStyle = t.border;
  ctx.lineWidth = st === "loading" ? 10 : 1.5;
  ctx.globalAlpha = st === "loading" ? 0.55 : 1;
  const g = st === "loading" ? 34 : 9;
  ctx.beginPath();
  for (let d = -s; d < s; d += g) {
    ctx.moveTo(x + d, y + s);
    ctx.lineTo(x + d + s, y);
  }
  ctx.stroke();
  ctx.restore();
}

function grid(ctx: CanvasRenderingContext2D, v: MapView, f: Frame, t: Tokens, labels: Label[]) {
  const step = v.s >= 2 ? 64 : v.s >= 0.5 ? 256 : v.s >= 0.125 ? 1024 : 4096;
  const [x0, z0] = toWorld(v, f, 0, 0);
  const [x1, z1] = toWorld(v, f, f.w, f.h);
  ctx.save();
  ctx.strokeStyle = t.ink;
  ctx.globalAlpha = 0.35;
  ctx.lineWidth = 1;
  for (let x = Math.ceil(x0 / step) * step; x < x1; x += step) {
    const [sx] = toScreen(v, f, x, 0);
    ctx.beginPath();
    ctx.moveTo(Math.round(sx) + 0.5, 0);
    ctx.lineTo(Math.round(sx) + 0.5, f.h);
    ctx.stroke();
    if (sx > 8 && sx < f.w - 70)
      labels.push({ txt: `X ${x}`, x: sx + 4, y: f.top + 6, pr: 9, left: true });
  }
  for (let z = Math.ceil(z0 / step) * step; z < z1; z += step) {
    const [, sy] = toScreen(v, f, 0, z);
    ctx.beginPath();
    ctx.moveTo(0, Math.round(sy) + 0.5);
    ctx.lineTo(f.w, Math.round(sy) + 0.5);
    ctx.stroke();
    if (sy > 30 && sy < f.h - f.bottom - 30)
      labels.push({ txt: `Z ${z}`, x: 6, y: sy + 4, pr: 9, left: true });
  }
  ctx.restore();
}

function structGlyph(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  kind: string,
  t: Tokens,
  on: boolean,
) {
  ctx.save();
  ctx.fillStyle = t.halo;
  ctx.beginPath();
  ctx.roundRect?.(x - 12, y - 12, 24, 24, 7);
  ctx.fill();
  ctx.strokeStyle = t.amber;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.roundRect?.(x - 10.5, y - 10.5, 21, 21, 6);
  ctx.stroke();
  const path = glyphPath(structureInfo(kind).glyph);
  if (path) {
    ctx.translate(x - 7.5, y - 7.5);
    ctx.scale(15 / 24, 15 / 24);
    ctx.strokeStyle = t.ink;
    ctx.lineWidth = 2.2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke(path);
  }
  ctx.restore();
  if (on) ring(ctx, x, y, 18, t);
}

function ring(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, t: Tokens) {
  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 5;
  ctx.stroke();
  ctx.strokeStyle = t.ink;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.restore();
}

function clusterBadge(ctx: CanvasRenderingContext2D, c: Cluster<StructMarker>, t: Tokens) {
  ctx.save();
  ctx.beginPath();
  ctx.arc(c.sx, c.sy, 16, 0, Math.PI * 2);
  ctx.fillStyle = t.halo;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(c.sx, c.sy, 13, 0, Math.PI * 2);
  ctx.lineWidth = 2;
  ctx.strokeStyle = t.ink;
  ctx.stroke();
  ctx.fillStyle = t.ink;
  ctx.font = "700 12px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(String(c.members.length), c.sx, c.sy + 0.5);
  ctx.restore();
}

function playerDisc(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  p: MinecraftMapPlayer,
  col: string,
  t: Tokens,
  on: boolean,
) {
  ctx.save();
  if (p.online && !reducedMotion()) {
    const ph = (performance.now() % 1600) / 1600;
    ctx.globalAlpha = (1 - ph) * 0.5;
    ctx.beginPath();
    ctx.arc(x, y, 10 + ph * 12, 0, Math.PI * 2);
    ctx.fillStyle = col;
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  ctx.beginPath();
  ctx.arc(x, y, 12, 0, Math.PI * 2);
  ctx.fillStyle = t.halo;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(x, y, 9.5, 0, Math.PI * 2);
  if (p.online) {
    ctx.fillStyle = col;
    ctx.fill();
    ctx.fillStyle = t.inkOn;
  } else {
    ctx.strokeStyle = col;
    ctx.lineWidth = 2.5;
    ctx.stroke();
    ctx.fillStyle = col;
  }
  ctx.font = "700 11px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(initialOf(p.gamertag), x, y + 0.5);
  ctx.restore();
  if (on) ring(ctx, x, y, 17, t);
}

function spotMark(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  s: number,
  pinned: boolean,
  t: Tokens,
) {
  ctx.save();
  if (pinned && s >= 2) {
    for (const [col, lw] of [
      [t.halo, 4],
      [t.ink, 2],
    ] as const) {
      ctx.strokeStyle = col;
      ctx.lineWidth = lw;
      ctx.strokeRect(x - s / 2 - 1, y - s / 2 - 1, s + 2, s + 2);
    }
  }
  ctx.beginPath();
  ctx.arc(x, y, 12, 0, Math.PI * 2);
  for (const [a, b, c, d] of [
    [-20, 0, -14, 0],
    [14, 0, 20, 0],
    [0, -20, 0, -14],
    [0, 14, 0, 20],
  ] as const) {
    ctx.moveTo(x + a, y + b);
    ctx.lineTo(x + c, y + d);
  }
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 5;
  ctx.stroke();
  ctx.strokeStyle = t.ink;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.restore();
}

interface ScaleRect extends Rect {
  bx: number;
  by: number;
  bw: number;
  txt: string;
}

function scaleRect(ctx: CanvasRenderingContext2D, s: number, f: Frame): ScaleRect {
  const bar = scaleBar(s);
  ctx.font = FONT;
  const tw = ctx.measureText(bar.label).width;
  const bx = 12;
  const by = f.h - f.bottom - 12;
  return {
    x: bx - 6,
    y: by - 24,
    w: Math.max(bar.px, tw) + 12 + 30,
    h: 32,
    bx,
    by,
    bw: bar.px,
    txt: bar.label,
  };
}

function drawScale(ctx: CanvasRenderingContext2D, r: ScaleRect, t: Tokens) {
  ctx.save();
  ctx.fillStyle = t.plate;
  ctx.beginPath();
  ctx.roundRect?.(r.x, r.y, r.w, r.h, 8);
  ctx.fill();
  ctx.strokeStyle = t.text;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(r.bx, r.by - 5);
  ctx.lineTo(r.bx, r.by + 1);
  ctx.lineTo(r.bx + r.bw, r.by + 1);
  ctx.lineTo(r.bx + r.bw, r.by - 5);
  ctx.stroke();
  ctx.fillStyle = t.text;
  ctx.font = FONT;
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  ctx.fillText(r.txt, r.bx, r.by - 8);
  ctx.font = "700 12px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText("N↑", r.x + r.w - 15, r.by - 2);
  ctx.restore();
}

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

/** The tapped thing's label first, then players, then structures; each clamped inside the
 *  visible map, moved below its marker if it collides, or dropped — never drawn under a
 *  control or another label. */
function placeLabels(
  ctx: CanvasRenderingContext2D,
  labels: Label[],
  keep: Rect[],
  f: Frame,
  t: Tokens,
) {
  const placed: Rect[] = [];
  const clash = (r: Rect) =>
    [...keep, ...placed].some(
      (q) => r.x < q.x + q.w && q.x < r.x + r.w && r.y < q.y + q.h && q.y < r.y + r.h,
    );
  const visH = f.h - f.bottom;
  ctx.save();
  ctx.font = FONT;
  for (const l of [...labels].sort((a, b) => a.pr - b.pr)) {
    if (!l.left && l.y > visH) continue; // its marker is under the panel

    const w = ctx.measureText(l.txt).width + 12;
    const h = 20;
    let r: Rect = l.left ? { x: l.x, y: l.y, w, h } : { x: l.x - w / 2, y: l.y - 26, w, h };
    r.x = Math.min(Math.max(r.x, 4), f.w - w - 4);
    r.y = Math.min(Math.max(r.y, 4), visH - h - 4);
    if (clash(r)) {
      const below = { ...r, y: l.y + 30 };
      if (!l.left && below.y + h < visH - 4 && !clash(below)) r = below;
      else if (l.pr >= 0) continue;
    }
    placed.push({ x: r.x - 2, y: r.y - 2, w: r.w + 4, h: r.h + 4 });
    ctx.fillStyle = t.plate;
    ctx.beginPath();
    ctx.roundRect?.(r.x, r.y, w, h, 6);
    ctx.fill();
    ctx.fillStyle = t.text;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(l.txt, r.x + 6, r.y + h / 2 + 0.5);
  }
  ctx.restore();
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
