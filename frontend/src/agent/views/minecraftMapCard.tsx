// minecraft_map — Minecraft_Dave's map card in a chat (DESIGN "`minecraft_map` tool-view";
// binding mock docs/mocks/minecraft-map/a-full-bleed.html, "Chat card"; plan §P3).
//
// The payload is data slots only: the world slot, the dimension, the centre, the zoom, the
// overlay switches and the one thing the card is about. The card builds every tile request
// itself from the on-box tile route (`minecraftTileUrl`, through the shared renderer) and
// reads nothing else from the payload — a URL-shaped field is never looked at, which is the
// carve-out the owner approved: no model-authored URL ever reaches the render.
//
// One finger scrolls the chat (`touch-action: pan-y`), two fingers pinch the card, a tap on
// a marker shows its coordinates, and a tap anywhere else opens the full map on this spot.

import { type ReactNode, type PointerEvent as ReactPointerEvent, useEffect, useMemo } from "react";
import { useCallback, useRef, useState } from "react";
import {
  ApiError,
  type MinecraftDim,
  type MinecraftMapPlayers,
  type MinecraftStructure,
  type MinecraftTimeline,
  type MinecraftTimelineEvent,
  api,
} from "../../api/client";
import {
  CastleIcon,
  CopyIcon,
  CrosshairIcon,
  ExpandIcon,
  MapIcon,
  PinIcon,
  RouteIcon,
  SkullIcon,
  UserIcon,
} from "../../components/icons";
import {
  type BaseLayer,
  type Frame,
  GLYPHS,
  type MapView,
  clamp,
  clusterMarkers,
  clusterTarget,
  deathCause,
  dimName,
  fmtClock,
  fmtDay,
  hitTest,
  overworldSide,
  pathUpTo,
  playersIn,
  scaleOfZoom,
  statusLine,
  structureInfo,
  tileKey,
  toScreen,
  toWorld,
  visibleTiles,
  zoomAt,
} from "../../minecraftMap";
import { openMinecraftMap } from "../../minecraftMapNav";
import {
  LEGEND,
  type Label,
  type Marker,
  type TileEntry,
  type Tokens,
  asSurvey,
  clusterBadge,
  colorOf,
  drawScale,
  drawTiles,
  fetchTile,
  line,
  markerGlyph,
  markerLabel,
  placeLabels,
  playerDisc,
  readTokens,
  scaleRect,
  spotMark,
  stars,
  structGlyph,
} from "../../screens/minecraftMapDraw";

// ---- the payload: every slot checked, everything else ignored ----

export type McFocusKind = "death" | "player" | "place" | "structure" | "spot";
export type McOverlay = "trails" | "deaths" | "structures" | "players";

export interface McFocus {
  kind: McFocusKind;
  label: string;
  x: number;
  y: number | null;
  z: number;
  /** When it happened (epoch s): a death, or where a player was last seen. */
  at: number | null;
  cause: string | null;
  killer: string | null;
}

export interface McMapCard {
  slot: string;
  world: string;
  loaded: boolean;
  dim: MinecraftDim;
  x: number;
  z: number;
  zoom: number;
  overlays: Record<McOverlay, boolean>;
  focus: McFocus | null;
  /** Whose travel log the trail and deaths come from, and over when (null: the last day). */
  trail: { player: string; gamertag: string; from: number | null; to: number | null } | null;
}

const DIM_IDS: MinecraftDim[] = ["overworld", "nether", "the_end"];
const FOCUS_KINDS: McFocusKind[] = ["death", "player", "place", "structure", "spot"];
const OVERLAY_IDS: McOverlay[] = ["trails", "deaths", "structures", "players"];
/** The world's edge: Bedrock's ±30 million blocks. */
const WORLD_MAX = 30_000_000;
const LABEL_MAX = 40;
const SLOT_RE = /^slot[1-9]$/;
/** The tile route's zooms: 4 is one pixel per chunk, -3 eight pixels per block. */
const ZOOM_MIN = -3;
const ZOOM_MAX = 4;
const DAY_S = 86_400;

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const coord = (v: unknown): number | null => {
  const n = num(v);
  return n === null ? null : clamp(Math.round(n), -WORLD_MAX, WORLD_MAX);
};
const str = (v: unknown, max: number): string =>
  typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, max) : "";
const obj = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;

/** The card's slots, each checked and clamped; null when there is no world to draw. Only
 *  these fields are read — anything else in the payload, a URL above all, is ignored. */
export function parseMinecraftMap(data: Record<string, unknown>): McMapCard | null {
  const slot = typeof data.slot === "string" && SLOT_RE.test(data.slot) ? data.slot : null;
  if (!slot) return null;
  const dim = DIM_IDS.find((d) => d === data.dim) ?? "overworld";
  const f = obj(data.focus);
  let focus: McFocus | null = null;
  const fx = coord(f?.x);
  const fz = coord(f?.z);
  const kind = FOCUS_KINDS.find((k) => k === f?.kind);
  if (f && kind && fx !== null && fz !== null) {
    const y = num(f.y);
    focus = {
      kind,
      label: str(f.label, LABEL_MAX),
      x: fx,
      y: y === null ? null : Math.round(y),
      z: fz,
      at: num(f.at),
      cause: str(f.cause, 40) || null,
      killer: str(f.killer, 60) || null,
    };
  }
  const c = obj(data.center);
  const ov = obj(data.overlays);
  const overlays = Object.fromEntries(
    OVERLAY_IDS.map((k) => [k, ov ? ov[k] === true : k !== "players"]),
  ) as Record<McOverlay, boolean>;
  const t = obj(data.trail);
  const player = str(t?.player, 64);
  const zoom = num(data.zoom);
  return {
    slot,
    world: str(data.world, 60),
    loaded: data.loaded === true,
    dim,
    x: coord(c?.x) ?? focus?.x ?? 0,
    z: coord(c?.z) ?? focus?.z ?? 0,
    zoom: zoom === null ? 1 : clamp(Math.round(zoom), ZOOM_MIN, ZOOM_MAX),
    overlays,
    focus,
    trail:
      t && player
        ? { player, gamertag: str(t.gamertag, 32), from: num(t.from), to: num(t.to) }
        : null,
  };
}

// ---- what the card says about the thing it shows ----

interface Shown {
  icon: ReactNode;
  tone: "" | "death" | "st";
  title: string;
  sub: string;
  x: number;
  y: number | null;
  z: number;
  dim: MinecraftDim;
}

function focusShown(f: McFocus, dim: MinecraftDim, who: string, now: number): Shown {
  const when = f.at ? `${fmtDay(f.at, now)}, ${fmtClock(f.at)} · ` : "";
  const at = { x: f.x, y: f.y, z: f.z, dim };
  if (f.kind === "death") {
    const title = who ? `${who} ${deathCause(f.cause, f.killer)}` : f.label || "A death";
    return { ...at, icon: <SkullIcon size={18} />, tone: "death", title, sub: when + dimName(dim) };
  }
  if (f.kind === "player") {
    return {
      ...at,
      icon: <UserIcon size={18} />,
      tone: "",
      title: f.label || who || "Last seen",
      sub: `${f.at ? "Last seen " : ""}${when}${dimName(dim)}`,
    };
  }
  if (f.kind === "place") {
    return {
      ...at,
      icon: <PinIcon size={18} />,
      tone: "",
      title: f.label || "Saved place",
      sub: `Saved place · ${dimName(dim)}`,
    };
  }
  if (f.kind === "structure") {
    return {
      ...at,
      icon: <CastleIcon size={18} />,
      tone: "st",
      title: f.label || "Structure",
      sub: `Located by the server · ${dimName(dim)}`,
    };
  }
  return {
    ...at,
    icon: <CrosshairIcon size={18} />,
    tone: "",
    title: f.label || "This spot",
    sub: dimName(dim),
  };
}

function markerShown(m: Marker, dim: MinecraftDim, now: number): Shown | null {
  if (m.type === "struct") {
    const s = structureInfo(m.s.structure);
    return {
      icon: (
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
      ),
      tone: "st",
      title: s.label,
      sub: `Located by the server · ${dimName(dim)}`,
      x: m.s.x,
      y: null,
      z: m.s.z,
      dim,
    };
  }
  if (m.type === "event" && m.e.kind === "death") {
    const e = m.e;
    return {
      icon: <SkullIcon size={18} />,
      tone: "death",
      title: `${e.gamertag} ${deathCause(e.cause, e.killer)}`,
      sub: `${fmtDay(e.at, now)}, ${fmtClock(e.at)} · ${dimName(dim)}`,
      x: Math.round(e.x ?? 0),
      y: e.y === null ? null : Math.round(e.y),
      z: Math.round(e.z ?? 0),
      dim,
    };
  }
  return null;
}

// ---- the card ----

type Hit =
  | { kind: "focus"; sx: number; sy: number }
  | { kind: "marker"; sx: number; sy: number; m: Marker }
  | { kind: "cluster"; sx: number; sy: number; members: Marker[] };

const CHIPS: { id: McOverlay; label: string }[] = [
  { id: "trails", label: "Trail" },
  { id: "deaths", label: "Deaths" },
  { id: "structures", label: "Structures" },
  { id: "players", label: "Players" },
];

const TILES_IN_FLIGHT = 4;
const MOVE_SLOP_PX = 8;
const COPIED_MS = 2000;
const LOADING = { tone: "spin", head: "Loading the map…", sub: "Asking the box" } as const;

const isUnreachable = (err: unknown) =>
  !(err instanceof ApiError) || err.status >= 502 || err.status === 0;

export function MinecraftMapCard({ data }: { data: Record<string, unknown> }): ReactNode {
  const card = useMemo(() => parseMinecraftMap(data), [data]);
  if (!card) {
    return <div className="tv-mc-empty">This map can&apos;t be shown.</div>;
  }
  return <McCard card={card} />;
}

function McCard({ card }: { card: McMapCard }): ReactNode {
  const { slot, dim, focus, trail } = card;
  const [ov, setOv] = useState(card.overlays);
  const [picked, setPicked] = useState<Shown | null>(null);
  const [players, setPlayers] = useState<MinecraftMapPlayers | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [structs, setStructs] = useState<MinecraftStructure[]>([]);
  const [log, setLog] = useState<MinecraftTimeline | null>(null);
  const [tiles, setTiles] = useState({ visible: 0, pending: 0 });
  const [copied, setCopied] = useState<string | null>(null);
  // The card is a picture of a moment: "now" is when it was drawn, for its day words.
  const [now] = useState(() => Date.now() / 1000);

  const mapEl = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const view = useRef<MapView>({ x: card.x + 0.5, z: card.z + 0.5, s: scaleOfZoom(card.zoom) });
  const frame = useRef<Frame>({ w: 1, h: 1, bottom: 0, top: 0 });
  const dirty = useRef(true);
  const hits = useRef<Hit[]>([]);
  const cache = useRef(new Map<string, TileEntry>());
  const inFlight = useRef(0);
  const tk = useRef<Tokens | null>(null);
  const state = useRef({ ov, players, structs, log });
  state.current = { ov, players, structs, log };

  const redraw = useCallback(() => {
    dirty.current = true;
  }, []);
  // biome-ignore lint/correctness/useExhaustiveDependencies: any change to what's drawn redraws.
  useEffect(redraw, [ov, players, structs, log]);

  // ---- data: the box's own routes, never anything the payload named ----

  useEffect(() => {
    let gone = false;
    api
      .minecraftMapPlayers(slot)
      .then((p) => {
        if (!gone) setPlayers(p);
      })
      .catch((err) => {
        if (!gone && isUnreachable(err)) setUnreachable(true);
      });
    return () => {
      gone = true;
    };
  }, [slot]);

  useEffect(() => {
    if (!ov.structures) return;
    let gone = false;
    api
      .minecraftMapStructures(slot, dim)
      .then((s) => {
        if (!gone) setStructs(s.dimension === dim ? s.structures : []);
      })
      .catch(() => {
        if (!gone) setStructs([]);
      });
    return () => {
      gone = true;
    };
  }, [slot, dim, ov.structures]);

  const wantLog = !!trail && (ov.trails || ov.deaths);
  useEffect(() => {
    if (!wantLog || !trail) return;
    let gone = false;
    const to = trail.to ?? now;
    const from = trail.from ?? to - DAY_S;
    api
      .minecraftTimeline(slot, Math.min(from, to), to, {
        include: ["trails", "events"],
        players: [trail.player],
      })
      .then((got) => {
        if (!gone) setLog(got);
      })
      .catch(() => {
        if (!gone) setLog(null);
      });
    return () => {
      gone = true;
    };
  }, [slot, trail, wantLog, now]);

  // ---- canvas ----

  useEffect(() => {
    const el = mapEl.current;
    if (!el) return;
    const fit = () => {
      const r = el.getBoundingClientRect();
      frame.current = { w: Math.max(1, r.width), h: Math.max(1, r.height), bottom: 0, top: 0 };
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
      const have = cache.current.get(key);
      const retry = have?.state === "failed" && Date.now() - have.at > 15_000;
      if (have && !retry) return have;
      if (inFlight.current >= TILES_IN_FLIGHT) return have ?? null;
      const entry: TileEntry = { img: null, at: Date.now(), state: "loading" };
      cache.current.set(key, entry);
      inFlight.current++;
      fetchTile(slot, dim, t.zoom, t.tx, t.tz, layer)
        .then((img) => cache.current.set(key, { img, at: Date.now(), state: "ok" }))
        .catch(() => cache.current.set(key, { img: null, at: Date.now(), state: "failed" }))
        .finally(() => {
          inFlight.current--;
          redraw();
        });
      return entry;
    },
    [slot, dim, redraw],
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
    if (dim === "the_end") stars(ctx, f, t);

    ctx.imageSmoothingEnabled = false;
    const spots = visibleTiles(v, f);
    const pending = drawTiles(
      ctx,
      spots,
      "surface",
      (s, layer) => cache.current.get(tileKey(slot, dim, s, layer)),
      (s, layer) => want(tileKey(slot, dim, s, layer), s, layer),
      t,
    );
    setTiles((p) =>
      p.visible === spots.length && p.pending === pending ? p : { visible: spots.length, pending },
    );

    const labels: Label[] = [];
    const next: Hit[] = [];
    const off = (sx: number, sy: number) => sx < -30 || sy < -30 || sx > f.w + 30 || sy > f.h + 30;
    const steel = colorOf(t, "--steel");

    if (st.ov.trails && st.log?.trails) {
      for (const tr of st.log.trails) {
        for (const seg of tr.segments) {
          if (seg.dim !== dim) continue;
          line(ctx, v, f, pathUpTo(seg, Number.POSITIVE_INFINITY), steel, t, "full");
        }
      }
    }

    const markers: Marker[] = [];
    if (st.ov.structures) {
      for (const s of st.structs) {
        const [sx, sy] = toScreen(v, f, s.x + 0.5, s.z + 0.5);
        if (!off(sx, sy)) markers.push({ type: "struct", s, x: s.x, z: s.z, sx, sy, pr: 5 });
      }
    }
    const isFocus = (e: MinecraftTimelineEvent) =>
      focus?.kind === "death" && focus.at !== null && Math.abs(e.at - focus.at) < 2;
    if (st.ov.deaths && st.log?.events) {
      for (const e of st.log.events) {
        if (e.kind !== "death" || e.dim !== dim || e.x === null || e.z === null) continue;
        if (isFocus(e)) continue; // the focus draws itself, on top
        const [sx, sy] = toScreen(v, f, e.x + 0.5, e.z + 0.5);
        if (!off(sx, sy)) markers.push({ type: "event", e, x: e.x, z: e.z, sx, sy, pr: 1 });
      }
    }
    for (const cl of clusterMarkers(markers)) {
      const one = cl.members[0];
      if (cl.members.length === 1 && one) {
        markerGlyph(ctx, one, t, false);
        const txt = markerLabel(one);
        if (txt && v.s >= 0.25) labels.push({ txt, x: one.sx, y: one.sy - 8, pr: one.pr });
        next.push({ kind: "marker", sx: one.sx, sy: one.sy, m: one });
      } else {
        clusterBadge(ctx, cl, t);
        next.push({ kind: "cluster", sx: cl.sx, sy: cl.sy, members: cl.members });
      }
    }

    if (st.ov.players && st.players) {
      for (const p of playersIn(st.players.players, dim)) {
        const [sx, sy] = toScreen(v, f, p.x, p.z);
        if (off(sx, sy)) continue;
        playerDisc(ctx, sx, sy, p, steel, t, false, false);
        labels.push({ txt: p.gamertag, x: sx, y: sy - 8, pr: 0 });
      }
    }

    // The thing the card is about, on top, with its label first in line.
    if (focus) {
      const [sx, sy] = toScreen(v, f, focus.x + 0.5, focus.z + 0.5);
      if (focus.kind === "death") {
        const e: MinecraftTimelineEvent = {
          kind: "death",
          xuid: trail?.player ?? "",
          gamertag: trail?.gamertag ?? "",
          at: focus.at ?? 0,
          dim,
          x: focus.x,
          y: focus.y,
          z: focus.z,
          cause: focus.cause,
          killer: focus.killer,
        };
        markerGlyph(ctx, { type: "event", e, x: focus.x, z: focus.z, sx, sy, pr: -1 }, t, true);
      } else if (focus.kind === "player") {
        const p = {
          xuid: trail?.player ?? "",
          gamertag: focus.label || trail?.gamertag || "?",
          dim,
          x: focus.x,
          y: focus.y ?? 0,
          z: focus.z,
          at: focus.at ?? 0,
          online: false,
        };
        playerDisc(ctx, sx, sy, p, steel, t, true, false);
      } else if (focus.kind === "structure") {
        structGlyph(ctx, sx, sy, focus.label.toLowerCase().replace(/\s+/g, "_"), t, true);
      } else {
        spotMark(ctx, sx, sy, v.s, false, t);
      }
      if (focus.label) labels.push({ txt: focus.label, x: sx, y: sy - 8, pr: -1 });
      next.push({ kind: "focus", sx, sy });
    }

    const scale = scaleRect(ctx, v.s, f);
    placeLabels(ctx, labels, [scale], f, t);
    drawScale(ctx, scale, t);
    hits.current = next;
  }, [want, dim, slot, focus, trail]);

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

  // ---- gestures: one finger is the chat's, two pinch, a tap picks or opens ----

  const ptrs = useRef(new Map<number, { x: number; y: number; x0: number; y0: number }>());
  const gesture = useRef({
    moved: false,
    pinched: false,
    pinch: null as null | { d: number; s: number },
  });

  const local = (x: number, y: number): [number, number] => {
    const r = mapEl.current?.getBoundingClientRect();
    return [x - (r?.left ?? 0), y - (r?.top ?? 0)];
  };

  function onPointerDown(e: ReactPointerEvent<HTMLDivElement>) {
    if (ptrs.current.size === 0) gesture.current = { moved: false, pinched: false, pinch: null };
    ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY });
    if (ptrs.current.size === 2) {
      const [a, b] = [...ptrs.current.values()];
      if (a && b) {
        gesture.current.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, s: view.current.s };
        gesture.current.pinched = true;
      }
    }
  }

  function onPointerMove(e: ReactPointerEvent<HTMLDivElement>) {
    const p = ptrs.current.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x;
    const dy = e.clientY - p.y;
    p.x = e.clientX;
    p.y = e.clientY;
    const g = gesture.current;
    if (Math.hypot(p.x - p.x0, p.y - p.y0) > MOVE_SLOP_PX) g.moved = true;
    if (ptrs.current.size >= 2 && g.pinch) {
      const [a, b] = [...ptrs.current.values()];
      if (!a || !b) return;
      const [mx, my] = local((a.x + b.x) / 2, (a.y + b.y) / 2);
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const factor = (g.pinch.s * dist) / g.pinch.d / view.current.s;
      view.current = zoomAt(view.current, frame.current, factor, mx, my);
      redraw();
    } else if (e.pointerType === "mouse" && g.moved) {
      // A mouse drag has no chat scroll to give way to; a finger's drag is the chat's.
      view.current = {
        ...view.current,
        x: view.current.x - dx / view.current.s,
        z: view.current.z - dy / view.current.s,
      };
      redraw();
    }
  }

  function onPointerUp(e: ReactPointerEvent<HTMLDivElement>) {
    if (!ptrs.current.delete(e.pointerId)) return;
    const g = gesture.current;
    if (ptrs.current.size < 2) g.pinch = null;
    // A cancel is the browser taking the gesture for the chat's scroll: never a tap.
    if (e.type !== "pointerup" || g.moved || g.pinched || ptrs.current.size > 0) return;
    const [sx, sy] = local(e.clientX, e.clientY);
    tap(sx, sy);
  }

  useEffect(() => {
    const el = mapEl.current;
    if (!el) return;
    // A trackpad pinch arrives as ctrl+wheel; a plain wheel scrolls the chat.
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const r = el.getBoundingClientRect();
      view.current = zoomAt(
        view.current,
        frame.current,
        e.deltaY < 0 ? 1.25 : 0.8,
        e.clientX - r.left,
        e.clientY - r.top,
      );
      redraw();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [redraw]);

  function openFull() {
    const [cx, cz] = toWorld(view.current, frame.current, frame.current.w / 2, frame.current.h / 2);
    const zoom = clamp(Math.round(-Math.log2(view.current.s)), ZOOM_MIN, ZOOM_MAX);
    openMinecraftMap(
      focus
        ? { slot, dim, x: focus.x, z: focus.z, zoom, mark: true }
        : { slot, dim, x: Math.floor(cx), z: Math.floor(cz), zoom, mark: false },
    );
  }

  function tap(sx: number, sy: number) {
    const hit = hitTest(hits.current, sx, sy);
    if (hit?.kind === "focus") return setPicked(null);
    if (hit?.kind === "marker") {
      const shown = markerShown(hit.m, dim, now);
      if (shown) return setPicked(shown);
    }
    if (hit?.kind === "cluster") {
      const to = clusterTarget(hit.members, view.current.s);
      if (to) {
        view.current = to;
        redraw();
        return;
      }
    }
    openFull();
  }

  async function copy(x: number, z: number) {
    const txt = `${x} ${z}`;
    try {
      await navigator.clipboard.writeText(txt);
      setCopied(`Copied X Z: ${txt}`);
    } catch {
      setCopied(`Couldn't reach the clipboard — ${txt}`);
    }
  }
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(null), COPIED_MS);
    return () => clearTimeout(t);
  }, [copied]);

  const shown = picked ?? (focus ? focusShown(focus, dim, trail?.gamertag ?? "", now) : null);
  // Until the box says who's on, the card claims nothing about being live.
  const status =
    !players && !unreachable
      ? LOADING
      : statusLine({
          live: players?.live ?? false,
          loaded: card.loaded,
          online: players ? players.players.filter((p) => p.online).length : 0,
          worldName: card.world || "This world",
          lastPlayed: null,
          unreachable,
          tilesVisible: tiles.visible,
          tilesPending: tiles.pending,
          dim,
          explored: true,
        });
  const key = LEGEND[dim][0]?.[1] ?? [128, 128, 128];
  const title = `${card.world || `Slot ${slot.slice(4)}`} · ${dimName(dim)}`;
  const nether = shown?.dim === "nether";
  const [ox, oz] = shown ? overworldSide(shown.x, shown.z) : [0, 0];

  return (
    <div className="tv-mc">
      <div className="tv-mc-head">
        <MapIcon size={16} />
        <span className="tv-mc-title">
          <b>{title}</b> · {dim === "nether" ? "Biomes" : "Surface"}
        </span>
        <button
          type="button"
          className="tv-mc-expand"
          aria-label="Open the full map"
          onClick={openFull}
        >
          <ExpandIcon size={20} />
        </button>
      </div>
      <div
        className="mc-map-card"
        ref={mapEl}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        role="img"
        aria-label={`Map of ${title}${shown ? `, showing ${shown.title} at X ${shown.x}, Z ${shown.z}` : ""}. Pinch to zoom; tap to open the full map.`}
      >
        <canvas ref={canvas} className="mc-map-canvas" />
      </div>
      <div className="tv-mc-foot">
        <p className={`tv-mc-status ${status.tone}`}>
          <span
            className={`mc-dot mc-dot-${status.tone === "spin" ? "ok" : status.tone}`}
            aria-hidden="true"
          />
          <span>
            <b>{status.head}</b> · {status.sub}
          </span>
        </p>
        {shown && (
          <div className="tv-mc-det">
            <div className="tv-mc-det-h">
              <span
                className={`tv-mc-det-ic${shown.tone ? ` ${shown.tone}` : ""}`}
                aria-hidden="true"
              >
                {shown.icon}
              </span>
              <div className="tv-mc-det-tx">
                <div className="tv-mc-det-t">{shown.title}</div>
                <div className="tv-mc-det-s">{shown.sub}</div>
              </div>
            </div>
            <div className="tv-mc-coord">
              <span className="tv-mc-xyz">
                X <b>{shown.x}</b>
                {shown.y !== null && (
                  <>
                    {" "}
                    · Y <b>{shown.y}</b>
                  </>
                )}{" "}
                · Z <b>{shown.z}</b>
              </span>
              <button
                type="button"
                className="tv-mc-copy"
                onClick={() => void copy(shown.x, shown.z)}
              >
                <CopyIcon size={18} />
                Copy X Z
              </button>
            </div>
            {nether && (
              <div className="tv-mc-alt">
                Overworld side:{" "}
                <b>
                  X {ox} · Z {oz}
                </b>
              </div>
            )}
          </div>
        )}
        {copied && (
          <output className="tv-mc-copied" aria-live="polite">
            {copied}
          </output>
        )}
        <fieldset className="tv-mc-tog" aria-label="Show on the map">
          {CHIPS.filter((c) => c.id !== "trails" || trail).map((c) => (
            <button
              key={c.id}
              type="button"
              className="tv-mc-chip"
              aria-pressed={ov[c.id]}
              onClick={() => setOv((o) => ({ ...o, [c.id]: !o[c.id] }))}
            >
              {c.label}
            </button>
          ))}
        </fieldset>
        <div className="tv-mc-lg">
          <span className="tv-mc-lg-i">
            <i style={{ background: `rgb(${key.join(",")})` }} />
            Explored
          </span>
          <span className="tv-mc-lg-i">
            <i style={{ background: `rgb(${asSurvey(key)})` }} />
            Satellite survey
          </span>
          {ov.trails && trail && (
            <span className="tv-mc-lg-i">
              <RouteIcon size={14} />
              Trail
            </span>
          )}
          <span className="tv-mc-lg-i death">
            <SkullIcon size={14} />
            <span>Death</span>
          </span>
          <span className="tv-mc-lg-i">
            <CastleIcon size={14} />
            Structure
          </span>
          {ov.players && (
            <span className="tv-mc-lg-i">
              <UserIcon size={14} />
              Player
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
