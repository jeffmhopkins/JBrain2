// The Minecraft map's geometry and words (binding mock docs/mocks/minecraft-map/a-full-bleed.html,
// plan §M8), kept pure so panning, zooming, clustering and the status line are tested without
// a canvas. The screen (screens/MinecraftMap.tsx) only draws what these return.
//
// Coordinates: world X grows east, world Z grows south, so north (−Z) is up on screen. A view
// is the world point at the middle of the visible map and a scale in screen px per block.

import type {
  MinecraftDim,
  MinecraftMapPlayer,
  MinecraftMapPoint,
  MinecraftSession,
  MinecraftSlot,
  MinecraftTimeline,
  MinecraftTimelineEvent,
  MinecraftTrail,
  MinecraftTrailSegment,
  MinecraftTricorderTarget,
} from "./api/client";

/** Tile side in px, and in blocks at zoom 0. */
export const TILE = 256;
/** Server zoom k is one pixel per 2^k blocks; 4 (one pixel per chunk) is the furthest out. */
export const MAX_ZOOM = 4;
/** 1 px per chunk … 8 px per block. Past 1 px per block the client scales tiles up itself,
 *  nearest-neighbour, so a block stays a crisp square. */
export const S_MIN = 1 / 16;
export const S_MAX = 8;
/** Point markers closer than this on screen merge into a count badge (the 44-px tap floor). */
export const CLUSTER_PX = 40;
/** A tap within this many px of a marker picks it. */
export const HIT_PX = 23;
/** A long-press pin glides in to at least this, so the one block it marks is visible. */
export const PIN_SCALE = 4;

export const DIMS: { id: MinecraftDim; label: string; name: string }[] = [
  { id: "overworld", label: "Overworld", name: "Overworld" },
  { id: "nether", label: "Nether", name: "Nether" },
  { id: "the_end", label: "End", name: "The End" },
];

export const dimName = (d: MinecraftDim) => DIMS.find((x) => x.id === d)?.name ?? d;

export interface MapView {
  x: number;
  z: number;
  s: number;
}

/** The map element's size and what covers it: the bottom panel and the floating top row. */
export interface Frame {
  w: number;
  h: number;
  bottom: number;
  top: number;
}

export const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v);
export const clampScale = (s: number) => clamp(s, S_MIN, S_MAX);

/** The view scale (px per block) for a tile-route zoom: 0 is 1 px per block, 4 one per
 *  chunk, -3 eight per block. A chat card's zoom slot is in these terms. */
export const scaleOfZoom = (zoom: number) => clampScale(2 ** -zoom);

/** The visible map is what the panel leaves; centring aims at its middle, a little below the
 *  floating top row. */
function middle(f: Frame): [number, number] {
  return [f.w / 2, (f.h - f.bottom) / 2 + f.top / 2];
}

export function toScreen(v: MapView, f: Frame, x: number, z: number): [number, number] {
  const [mx, my] = middle(f);
  return [(x - v.x) * v.s + mx, (z - v.z) * v.s + my];
}

export function toWorld(v: MapView, f: Frame, sx: number, sy: number): [number, number] {
  const [mx, my] = middle(f);
  return [(sx - mx) / v.s + v.x, (sy - my) / v.s + v.z];
}

/** Zoom by `factor` keeping the world point under (sx, sy) where it is. */
export function zoomAt(v: MapView, f: Frame, factor: number, sx: number, sy: number): MapView {
  const [wx, wz] = toWorld(v, f, sx, sy);
  const s = clampScale(v.s * factor);
  const next = { ...v, s };
  const [nx, nz] = toWorld(next, f, sx, sy);
  return { x: v.x + wx - nx, z: v.z + wz - nz, s };
}

export function panBy(v: MapView, dx: number, dy: number): MapView {
  return { ...v, x: v.x - dx / v.s, z: v.z - dy / v.s };
}

/** The server zoom whose pixels are closest to (and not much smaller than) the screen's: a
 *  tile is never drawn below 0.75 of its own size, so zoomed out it fetches coarser tiles. */
export function zoomFor(s: number): number {
  let k = 0;
  while (k < MAX_ZOOM && s * (1 << k) < 0.75) k++;
  return k;
}

export interface TileSpot {
  zoom: number;
  tx: number;
  tz: number;
  sx: number;
  sy: number;
  size: number;
}

/** The tiles covering the map, nearest the middle first, so the middle fills in first. */
export function visibleTiles(v: MapView, f: Frame): TileSpot[] {
  const zoom = zoomFor(v.s);
  const span = TILE << zoom;
  const [x0, z0] = toWorld(v, f, 0, 0);
  const [x1, z1] = toWorld(v, f, f.w, f.h);
  const [mx, my] = middle(f);
  const out: (TileSpot & { d: number })[] = [];
  for (let tz = Math.floor(z0 / span); tz <= Math.floor(z1 / span); tz++) {
    for (let tx = Math.floor(x0 / span); tx <= Math.floor(x1 / span); tx++) {
      const [sx, sy] = toScreen(v, f, tx * span, tz * span);
      const size = span * v.s;
      out.push({
        zoom,
        tx,
        tz,
        sx,
        sy,
        size,
        d: Math.hypot(sx + size / 2 - mx, sy + size / 2 - my),
      });
    }
  }
  out.sort((a, b) => a.d - b.d);
  return out.map(({ d: _d, ...t }) => t);
}

/** The map's base layers the box renders: explored ground in its real block colours, or
 *  the biome atlas. Unexplored ground is the biome survey under either. */
export type BaseLayer = "surface" | "biomes";

export const tileKey = (
  slot: string,
  dim: MinecraftDim,
  t: { zoom: number; tx: number; tz: number },
  layer: BaseLayer = "surface",
) => `${slot}|${dim}|${layer}|${t.zoom}|${t.tx}|${t.tz}`;

/** The view that frames these world points in the visible map, or null for none. */
export function fitView(pts: [number, number][], f: Frame, pad = 56, maxS = 1.2): MapView | null {
  if (!pts.length) return null;
  const xs = pts.map((p) => p[0]);
  const zs = pts.map((p) => p[1]);
  const w = Math.max(...xs) - Math.min(...xs) || 1;
  const h = Math.max(...zs) - Math.min(...zs) || 1;
  const availW = Math.max(80, f.w - pad * 2);
  const availH = Math.max(60, f.h - f.bottom - f.top - pad * 2);
  return {
    x: (Math.min(...xs) + Math.max(...xs)) / 2,
    z: (Math.min(...zs) + Math.max(...zs)) / 2,
    s: clampScale(Math.min(maxS, availW / w, availH / h)),
  };
}

// ---- markers and clustering ----

export interface Placed {
  x: number;
  z: number;
  sx: number;
  sy: number;
  /** Lower draws (and labels) first: the tapped thing, players, places, spawn, structures. */
  pr: number;
}

export interface Cluster<T extends Placed> {
  sx: number;
  sy: number;
  members: T[];
}

/** Greedy, in priority order: each marker joins the first cluster seeded within `r` px. */
export function clusterMarkers<T extends Placed>(items: T[], r = CLUSTER_PX): Cluster<T>[] {
  const sorted = [...items].sort((a, b) => a.pr - b.pr);
  const out: Cluster<T>[] = [];
  for (const m of sorted) {
    const c = out.find((q) => Math.hypot(q.sx - m.sx, q.sy - m.sy) < r);
    if (c) c.members.push(m);
    else out.push({ sx: m.sx, sy: m.sy, members: [m] });
  }
  return out;
}

/** Where a tapped count badge zooms to: in until its members separate (at most ×8 a tap),
 *  or null when they share a block — then the first one opens instead. */
export function clusterTarget(members: { x: number; z: number }[], s: number): MapView | null {
  let minD = Number.POSITIVE_INFINITY;
  for (let i = 0; i < members.length; i++) {
    for (let j = i + 1; j < members.length; j++) {
      const a = members[i];
      const b = members[j];
      if (a && b) minD = Math.min(minD, Math.hypot(a.x - b.x, a.z - b.z));
    }
  }
  const want = clampScale(Math.min(Math.max(s * 2.2, 52 / Math.max(minD, 0.01)), s * 8));
  if (want <= s * 1.01) return null;
  const n = members.length || 1;
  return {
    x: members.reduce((a, q) => a + q.x, 0) / n,
    z: members.reduce((a, q) => a + q.z, 0) / n,
    s: want,
  };
}

/** The nearest hit within HIT_PX of a tap, or null. */
export function hitTest<T extends { sx: number; sy: number }>(
  hits: T[],
  sx: number,
  sy: number,
): T | null {
  let best: T | null = null;
  let bd = HIT_PX;
  for (const h of hits) {
    const d = Math.hypot(h.sx - sx, h.sy - sy);
    if (d < bd) {
      bd = d;
      best = h;
    }
  }
  return best;
}

// ---- scale bar ----

const fmtN = (n: number) => n.toLocaleString("en-US");

/** A round number of blocks at least 56 px long, and how many blocks a pixel is once that
 *  is two or more. */
export function scaleBar(s: number): { blocks: number; px: number; label: string } {
  const nice = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096];
  const blocks = nice.find((n) => n * s >= 56) ?? 4096;
  const bpp = Math.round(1 / s);
  const per = s <= S_MIN + 1e-6 ? " · 1 px = 1 chunk" : bpp >= 2 ? ` · 1 px = ${bpp} blocks` : "";
  return {
    blocks,
    px: blocks * s,
    label: `${fmtN(blocks)} ${blocks === 1 ? "block" : "blocks"}${per}`,
  };
}

// ---- structures: the server's `locate` names (deploy/minecraft/mapping.py STRUCTURES) ----

/** Lucide outlines as SVG path data, so the canvas glyphs match the chrome's icon set. */
export const GLYPHS = {
  house:
    "M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  castle:
    "M22 20v-9H2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2ZM18 11V4H6v7M15 22v-4a3 3 0 0 0-6 0v4M22 11V9M2 11V9M6 4V2M18 4V2M10 4V2M14 4V2",
  landmark: "M3 22h18M6 18v-7M10 18v-7M14 18v-7M18 18v-7M12 2l8 5H4Z",
  flag: "M4 22V4M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1",
  building:
    "M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18ZM6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2M10 6h4M10 10h4M10 14h4M10 18h4",
  swords:
    "M14.5 17.5 3 6V3h3l11.5 11.5M13 19l6-6M16 16l4 4M19 21l2-2M14.5 6.5 18 3h3v3l-3.5 3.5M5 14l4 4M7 17l-3 3M3 19l2 2",
  eye: "M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0M15 12a3 3 0 1 1-6 0 3 3 0 1 1 6 0",
  ship: "M12 10.189V14M12 2v3M19 13V7a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v6M19.38 20A11.6 11.6 0 0 0 21 14l-8.188-3.639a2 2 0 0 0-1.624 0L3 14a11.6 11.6 0 0 0 2.81 7.76M2 21c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1s1.2 1 2.5 1c2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1",
  gem: "M6 3h12l4 6-10 13L2 9ZM11 3 8 9l4 13 4-13-3-6M2 9h20",
  portal:
    "M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1zM10 7c2 1 2 3 0 5s-2 4 0 5M14 7c-2 1-2 3 0 5s2 4 0 5",
  login: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  // The No-Spawn Beacon and the wormhole gate's parts (§M26): the gate's frame fills
  // violet when it is open, so its swirl is drawn as its own stroke on top.
  shield:
    "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z",
  portalFrame: "M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z",
  swirl: "M10 7c2 1 2 3 0 5s-2 4 0 5M14 7c-2 1-2 3 0 5s2 4 0 5",
  pin: "M20 10c0 4.99-5.54 10.19-7.4 11.8a1 1 0 0 1-1.2 0C9.54 20.19 4 14.99 4 10a8 8 0 0 1 16 0",
  pick: "M14.531 12.469 6.619 20.38a1 1 0 1 1-3-3l7.912-7.912M15.686 4.314A12.5 12.5 0 0 0 5.461 2.958 1 1 0 0 0 5.58 4.71a22 22 0 0 1 6.318 3.393M17.7 3.7a1 1 0 0 0-1.4 0l-4.6 4.6a1 1 0 0 0 0 1.4l2.6 2.6a1 1 0 0 0 1.4 0l4.6-4.6a1 1 0 0 0 0-1.4zM19.686 8.314a12.501 12.501 0 0 1 1.356 10.225 1 1 0 0 1-1.751-.119 22 22 0 0 0-3.393-6.319",
} as const;

export type GlyphKey = keyof typeof GLYPHS;

export const STRUCTURES: Record<string, { label: string; glyph: GlyphKey }> = {
  village: { label: "Village", glyph: "house" },
  mansion: { label: "Woodland mansion", glyph: "castle" },
  monument: { label: "Ocean monument", glyph: "landmark" },
  pillager_outpost: { label: "Pillager outpost", glyph: "flag" },
  ancient_city: { label: "Ancient city", glyph: "building" },
  trail_ruins: { label: "Trail ruins", glyph: "landmark" },
  trial_chambers: { label: "Trial chambers", glyph: "swords" },
  stronghold: { label: "Stronghold", glyph: "eye" },
  temple: { label: "Temple", glyph: "landmark" },
  ruins: { label: "Ocean ruins", glyph: "landmark" },
  shipwreck: { label: "Shipwreck", glyph: "ship" },
  buried_treasure: { label: "Buried treasure", glyph: "gem" },
  ruined_portal: { label: "Ruined portal", glyph: "portal" },
  mineshaft: { label: "Mineshaft", glyph: "pick" },
  fortress: { label: "Nether fortress", glyph: "castle" },
  bastion_remnant: { label: "Bastion remnant", glyph: "castle" },
  end_city: { label: "End city", glyph: "building" },
};

export function structureInfo(kind: string): { label: string; glyph: GlyphKey } {
  return STRUCTURES[kind] ?? { label: kind.replace(/_/g, " "), glyph: "landmark" };
}

// ---- what a tapped thing says ----

export const SURVEY_WORD = "Satellite survey (predicted)";
const NB = " ";

export interface SpotText {
  title: string;
  sub: string;
  /** Y to show in the coordinates; null when only an estimate (it's in `sub` as `~Y`). */
  y: number | null;
  survey: boolean;
}

/** A tapped spot, from `/map/point`. Explored ground's y is real; the survey's is an
 *  estimate, so it reads `~Y` and stays out of the coordinates. */
export function spotText(p: MinecraftMapPoint | null, dim: MinecraftDim): SpotText {
  if (!p) return { title: "Looking…", sub: dimName(dim), y: null, survey: false };
  if (p.source === "unknown" || !p.biome) {
    return {
      title: dim === "the_end" ? "The void" : "Nothing to draw here",
      sub:
        dim === "the_end"
          ? `${dimName(dim)} · no ground here, not even in the survey`
          : `${dimName(dim)} · not explored, and no survey for this world`,
      y: null,
      survey: false,
    };
  }
  const survey = p.source === "survey";
  const y = p.y ?? null;
  const at = y === null ? "" : ` · ground at Y${NB}${survey ? "~" : ""}${y}`;
  return {
    title: p.biome,
    sub: `${survey ? SURVEY_WORD : "Explored"}${at}`,
    y: survey ? null : y,
    survey,
  };
}

/** The Nether's coordinates, as the Overworld spot they lead to (×8). */
export const overworldSide = (x: number, z: number) => [x * 8, z * 8] as const;

// ---- the honest status line ----

export type StatusTone = "ok" | "off" | "bad" | "spin" | "warn";

export interface StatusInput {
  /** The map's world is the loaded one and the server is running. */
  live: boolean;
  /** The map's world is the loaded one (running or not). */
  loaded: boolean;
  online: number;
  worldName: string;
  /** When the map's world was last played (epoch s), for a world that isn't loaded. */
  lastPlayed: number | null;
  /** The box stopped answering. */
  unreachable: boolean;
  tilesVisible: number;
  tilesPending: number;
  dim: MinecraftDim;
  /** Anyone has generated ground in this dimension. */
  explored: boolean;
}

/** The map never claims more than it knows: how far behind the world it is, and whether
 *  positions are live. */
export function statusLine(i: StatusInput): { tone: StatusTone; head: string; sub: string } {
  if (i.unreachable) {
    return {
      tone: "bad",
      head: "Box unreachable — retrying…",
      sub: "Pan and zoom still work over the tiles this phone has",
    };
  }
  if (!i.loaded) {
    const when = i.lastPlayed
      ? new Date(i.lastPlayed * 1000).toLocaleDateString("en-US", {
          month: "short",
          day: "numeric",
        })
      : null;
    return {
      tone: "off",
      head: `${i.worldName} isn't loaded`,
      sub: when
        ? `Map as of ${when}, when it was last played`
        : "Map as of when it was last played",
    };
  }
  if (!i.live) {
    return {
      tone: "off",
      head: "Server stopped",
      sub: "Map as of then · players where they logged off",
    };
  }
  const on = `Live · ${i.online} on`;
  if (i.tilesPending > 0) {
    return {
      tone: "spin",
      head: `Loading map tiles · ${i.tilesVisible - i.tilesPending} of ${i.tilesVisible}`,
      sub: `${on} · up to a minute behind`,
    };
  }
  if (i.dim === "the_end" && !i.explored) {
    return { tone: "ok", head: on, sub: "Nobody has been to the End — all of it is the survey" };
  }
  return { tone: "ok", head: on, sub: "Up to a minute behind the world · positions live" };
}

// ---- the world picker and the Minecraft screen's way in ----

export function slotLabel(s: MinecraftSlot): string {
  return s.name ?? `Slot ${s.id.replace(/\D/g, "")}`;
}

export function slotLine(s: MinecraftSlot, running: boolean): string {
  if (!s.exists) return "empty · no world here";
  if (s.active) return running ? "loaded · being played now" : "loaded · server stopped";
  if (!s.last_played) return "never played";
  const d = new Date(s.last_played * 1000).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
  });
  return `last played ${d}`;
}

/** The Minecraft screen's Map row: the loaded world and who's on it. */
export function entryLine(world: string | null, online: string[]): string {
  const who =
    online.length === 0
      ? "nobody on right now"
      : `${new Intl.ListFormat("en", { style: "long", type: "conjunction" }).format(online)} on the map`;
  return world ? `${world} · ${who}` : who;
}

/** Players in this dimension, online first; a player's label drops "offline" when live. */
export function playersIn(players: MinecraftMapPlayer[], dim: MinecraftDim): MinecraftMapPlayer[] {
  return players
    .filter((p) => p.dim === dim)
    .sort((a, b) => Number(b.online) - Number(a.online) || a.gamertag.localeCompare(b.gamertag));
}

/** The disc's letter. */
export const initialOf = (tag: string) => (tag[0] ?? "?").toUpperCase();

// ---- players' colours ----

/** Player colours: the four accents the mock gives its players, in roster order. */
export const PLAYER_TOKENS = ["--steel", "--green", "--amber", "--violet"];

export interface RosterEntry {
  xuid: string;
  gamertag: string;
  token: string;
}

/** Everyone the map knows of, by gamertag, each with a stable colour: the same player is
 *  the same colour on the map, the scrubber, the day strip and the session list. */
export function rosterOf(players: { xuid: string; gamertag: string }[]): RosterEntry[] {
  const seen = new Map<string, string>();
  for (const p of players) if (!seen.has(p.xuid)) seen.set(p.xuid, p.gamertag);
  return [...seen]
    .map(([xuid, gamertag]) => ({ xuid, gamertag }))
    .sort((a, b) => a.gamertag.localeCompare(b.gamertag) || a.xuid.localeCompare(b.xuid))
    .map((p, i) => ({ ...p, token: PLAYER_TOKENS[i % PLAYER_TOKENS.length] ?? "--steel" }));
}

// ---- the timeline (M8a): time in epoch seconds, days in the phone's own time zone ----

export type Preset = "session" | "today" | "week" | "all";

export const PRESETS: { id: Preset; label: string; name: string }[] = [
  { id: "session", label: "Session", name: "This session" },
  { id: "today", label: "Today", name: "Today" },
  { id: "week", label: "Week", name: "This week" },
  { id: "all", label: "All", name: "Everything" },
];

export const SPEEDS = [1, 10, 60, 600] as const;

/** One tap moves to the next speed, round to 1× again. */
export function nextSpeed(s: number): number {
  const i = SPEEDS.indexOf(s as (typeof SPEEDS)[number]);
  return SPEEDS[(i + 1) % SPEEDS.length] ?? 1;
}

const MIN = 60;
const HOUR = 3600;
const DAY = 86_400;
/** A loaded session's range runs this far past each end. */
export const SESSION_PAD = 2 * MIN;
/** A tapped day's range runs this far past its first start and last end. */
export const DAY_PAD = 10 * MIN;
/** How much of each player's recent path the fading tail shows. */
export const TAIL_S = 5 * MIN;
/** The server's longest range. */
export const MAX_SPAN = 92 * DAY;
/** The day strip covers the last week; the session list reaches further back. */
export const STRIP_DAYS = 7;
/** The travel log samples every 10 s, keeping a sample only after some movement. */
const SAMPLE_S = 10;

const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function fmtClock(sec: number): string {
  const d = new Date(sec * 1000);
  const h = d.getHours();
  return `${h % 12 || 12}:${String(d.getMinutes()).padStart(2, "0")}${NB}${h >= 12 ? "pm" : "am"}`;
}

/** Local midnight at the start of `sec`'s day, `shift` days on. */
export function dayStart(sec: number, shift = 0): number {
  const d = new Date(sec * 1000);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + shift).getTime() / 1000;
}

/** Whole calendar days from `sec` to `now`: 0 today, 1 yesterday. */
export function dayDiff(sec: number, now: number): number {
  return Math.round((dayStart(now) - dayStart(sec)) / DAY);
}

export function fmtShortDay(sec: number): string {
  const d = new Date(sec * 1000);
  return `${DOW[d.getDay()]}${NB}${MON[d.getMonth()]}${NB}${d.getDate()}`;
}

export function fmtDay(sec: number, now: number): string {
  const dd = dayDiff(sec, now);
  return dd === 0 ? "Today" : dd === 1 ? "Yesterday" : fmtShortDay(sec);
}

export function fmtDur(secs: number): string {
  const m = Math.round(secs / MIN);
  if (m < 60) return `${m}${NB}min`;
  return `${Math.floor(m / 60)}h${NB}${String(m % 60).padStart(2, "0")}m`;
}

/** "4:10–7:52 pm", "11:40 am–12:15 pm", or "1:58 pm–now" for a session still on. */
export function fmtSpan(start: number, end: number | null): string {
  const a = fmtClock(start);
  if (end === null) return `${a}–now`;
  const b = fmtClock(end);
  return a.slice(-2) === b.slice(-2) ? `${a.slice(0, -3)}–${b}` : `${a}–${b}`;
}

export function fmtKm(blocks: number): string {
  return `${(blocks / 1000).toFixed(1)}${NB}km`;
}

export const sessionEnd = (s: MinecraftSession, now: number) => s.end ?? now;

export const sessionDistance = (s: MinecraftSession) =>
  Object.values(s.distance).reduce((a, b) => a + (b ?? 0), 0);

/** Loading a session: its own span and two minutes either side, never past now. */
export function sessionRange(s: MinecraftSession, now: number): [number, number] {
  return [s.start - SESSION_PAD, Math.min(now, sessionEnd(s, now) + SESSION_PAD)];
}

/** A tapped day: its activity, from the first start to the last end. */
export function dayRange(list: MinecraftSession[], now: number): [number, number] | null {
  if (!list.length) return null;
  const a = Math.min(...list.map((s) => s.start));
  const b = Math.max(...list.map((s) => sessionEnd(s, now)));
  return [a - DAY_PAD, Math.min(now, b + DAY_PAD)];
}

export function presetRange(
  p: Preset,
  sessions: MinecraftSession[],
  now: number,
): [number, number] {
  if (p === "today") return [dayStart(now), now];
  if (p === "week") return [now - 7 * DAY, now];
  if (p === "all") {
    const first = sessions.length ? Math.min(...sessions.map((s) => s.start)) : now - 7 * DAY;
    return [Math.max(first - SESSION_PAD, now - MAX_SPAN + MIN), now];
  }
  // This session: whoever is on now, from the earliest of them; or the newest session.
  const open = sessions.filter((s) => s.end === null);
  if (open.length) return [Math.min(...open.map((s) => s.start)) - SESSION_PAD, now];
  const newest = [...sessions].sort((a, b) => b.start - a.start)[0];
  return newest ? sessionRange(newest, now) : [now - HOUR, now];
}

export interface StripDay {
  start: number;
  /** Today, or the weekday. */
  label: string;
  date: number;
  today: boolean;
  /** Who played that day, in roster order. */
  who: string[];
  /** That day's sessions, by start. */
  sessions: MinecraftSession[];
}

/** The days since the first session, at most the last `days`, oldest first. */
export function dayStrip(
  sessions: MinecraftSession[],
  now: number,
  roster: string[],
  days = STRIP_DAYS,
): StripDay[] {
  if (!sessions.length) return [];
  const first = dayStart(Math.min(...sessions.map((s) => s.start)));
  const out: StripDay[] = [];
  for (let k = days - 1; k >= 0; k--) {
    const start = dayStart(now, -k);
    if (start < first) continue;
    const next = dayStart(now, -k + 1);
    const list = sessions
      .filter((s) => s.start >= start && s.start < next)
      .sort((a, b) => a.start - b.start);
    const d = new Date(start * 1000);
    out.push({
      start,
      label: k === 0 ? "Today" : (DOW[d.getDay()] ?? ""),
      date: d.getDate(),
      today: k === 0,
      who: roster.filter((x) => list.some((s) => s.xuid === x)),
      sessions: list,
    });
  }
  return out;
}

export interface SessionGroup {
  start: number;
  label: string;
  list: MinecraftSession[];
}

/** The Sessions sheet: newest first, grouped by day, optionally one player's. */
export function sessionGroups(
  sessions: MinecraftSession[],
  who: string | null,
  now: number,
): SessionGroup[] {
  const out: SessionGroup[] = [];
  const list = sessions
    .filter((s) => who === null || s.xuid === who)
    .sort((a, b) => b.start - a.start);
  for (const s of list) {
    const start = dayStart(s.start);
    const g = out[out.length - 1];
    if (g && g.start === start) g.list.push(s);
    else {
      const dd = dayDiff(s.start, now);
      const label = `${fmtDay(s.start, now)}${dd < 2 ? ` · ${fmtShortDay(s.start)}` : ""}`;
      out.push({ start, label, list: [s] });
    }
  }
  return out;
}

/** Time mode is anywhere but now. */
export const isTimeTravel = (on: boolean, t: number, now: number) => on && t < now - 1;

/** The status line while replaying or scrubbed: it never says Live. */
export function timeStatus(
  t: number,
  now: number,
  playing: boolean,
  speed: number,
): { tone: StatusTone; head: string; sub: string } {
  const day = dayDiff(t, now) < 2 ? fmtDay(t, now).toLowerCase() : fmtDay(t, now);
  return {
    tone: "warn",
    head: `Not live · ${day} ${fmtClock(t)}`,
    sub: `${playing ? `Replaying at ${speed}×` : "Paused"} · positions from the travel log · terrain as of now`,
  };
}

// ---- keeping the trail and what it explored up with the players ----

/** Whether a disc is ahead of its trail: some player's newest sample is newer than the
 *  travel log the trails were drawn from. Positions are read every 10 s, the trails every
 *  minute, so without a catch-up the disc ran up to a minute ahead of its trail's end. */
export function trailBehind(players: { at: number }[], log: { now: number } | null): boolean {
  return !!log && players.some((p) => p.at > log.now);
}

/** The newest point of any trail in a travel log: it moves exactly when a trail grows. */
export function trailTail(log: MinecraftTimeline | null): number | null {
  let tail: number | null = null;
  for (const tr of log?.trails ?? []) {
    for (const seg of tr.segments) {
      const t = seg.points[seg.points.length - 1]?.[0];
      if (t !== undefined && (tail === null || t > tail)) tail = t;
    }
  }
  return tail;
}

/** The trail points newer than `since` in one dimension: the ground just walked. */
export function trailSince(
  log: MinecraftTimeline | null,
  since: number,
  dim: MinecraftDim,
): [number, number][] {
  const out: [number, number][] = [];
  for (const tr of log?.trails ?? []) {
    for (const seg of tr.segments) {
      if (seg.dim !== dim) continue;
      for (const p of seg.points) if (p[0] > since) out.push([p[1], p[3]]);
    }
  }
  return out;
}

/** Explored ground is what the server generated round a player — its view distance, not
 *  the fog's 4-chunk reveal — so a step can change the tiles this far off. */
export const EXPLORE_REACH = 16 * 16;

/** Every tile, at every zoom, within `reach` blocks of these points, as `zoom|tx|tz`: the
 *  tiles whose explored ground those steps could have changed. */
export function tilesNear(pts: [number, number][], reach: number): Set<string> {
  const out = new Set<string>();
  for (let zoom = 0; zoom <= MAX_ZOOM; zoom++) {
    const span = TILE << zoom;
    for (const [x, z] of pts) {
      for (let tz = Math.floor((z - reach) / span); tz <= Math.floor((z + reach) / span); tz++) {
        for (let tx = Math.floor((x - reach) / span); tx <= Math.floor((x + reach) / span); tx++) {
          out.add(`${zoom}|${tx}|${tz}`);
        }
      }
    }
  }
  return out;
}

// ---- where everyone was at a moment ----

export interface TrailPos {
  x: number;
  y: number;
  z: number;
  dim: MinecraftDim;
  session: string | null;
}

const lerp = (a: number, b: number, f: number) => a + (b - a) * f;

function within(seg: MinecraftTrailSegment, t: number, downsampled: boolean): TrailPos {
  const pts = seg.points;
  let lo = 0;
  let hi = pts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((pts[mid]?.[0] ?? 0) < t) lo = mid + 1;
    else hi = mid;
  }
  const q = pts[lo] ?? pts[pts.length - 1];
  const p = pts[Math.max(0, lo - 1)] ?? q;
  if (!p || !q) return { x: 0, y: 0, z: 0, dim: seg.dim, session: seg.session };
  const gap = q[0] - p[0];
  let f = gap > 0 ? (t - p[0]) / gap : 1;
  // A full-rate trail keeps a sample only after movement, so a long gap is standing still
  // followed by one sample's worth of walking — not a slow glide across the whole gap.
  if (!downsampled && gap > 3 * SAMPLE_S) f = Math.max(0, (t - (q[0] - SAMPLE_S)) / SAMPLE_S);
  f = clamp(f, 0, 1);
  return {
    x: lerp(p[1], q[1], f),
    y: lerp(p[2], q[2], f),
    z: lerp(p[3], q[3], f),
    dim: seg.dim,
    session: seg.session,
  };
}

const held = (seg: MinecraftTrailSegment): TrailPos | null => {
  const p = seg.points[seg.points.length - 1];
  return p ? { x: p[1], y: p[2], z: p[3], dim: seg.dim, session: seg.session } : null;
};

/** Where a player was at `t`: interpolated inside a segment, held at a segment's end until
 *  the same session goes on (through a portal, after a respawn) or while that session is
 *  still open; nowhere between sessions. */
export function posAt(
  trail: MinecraftTrail,
  t: number,
  isOpen: (session: string) => boolean = () => false,
): TrailPos | null {
  const segs = trail.segments;
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    const first = seg?.points[0];
    const last = seg?.points[seg.points.length - 1];
    if (!seg || !first || !last) continue;
    if (t < first[0]) {
      const prev = segs[i - 1];
      return prev && prev.session !== null && prev.session === seg.session ? held(prev) : null;
    }
    if (t <= last[0]) return within(seg, t, trail.downsampled);
  }
  const end = segs[segs.length - 1];
  return end?.session && isOpen(end.session) ? held(end) : null;
}

/** A segment's path up to `t` (and from `since`), ending at `t`'s own position when `t` is
 *  inside it: what the trail and the fading tail draw. */
export function pathUpTo(
  seg: MinecraftTrailSegment,
  t: number,
  since = Number.NEGATIVE_INFINITY,
  downsampled = false,
): [number, number][] {
  const out: [number, number][] = [];
  for (const p of seg.points) {
    if (p[0] > t) break;
    if (p[0] >= since) out.push([p[1], p[3]]);
  }
  const first = seg.points[0];
  const last = seg.points[seg.points.length - 1];
  if (first && last && t >= first[0] && t < last[0]) {
    const at = within(seg, t, downsampled);
    out.push([at.x, at.z]);
  }
  return out;
}

/** The session a player was in at `t`, if any. */
export function sessionAt(
  sessions: MinecraftSession[],
  xuid: string,
  t: number,
  now: number,
): MinecraftSession | null {
  return sessions.find((s) => s.xuid === xuid && s.start <= t && t <= sessionEnd(s, now)) ?? null;
}

// ---- events: the scrubber's glyphs and what they say ----

const SCRUB_KINDS = new Set(["join", "leave", "death", "dimension"]);

export interface ScrubMark {
  /** px from the track's left edge. */
  left: number;
  lead: MinecraftTimelineEvent;
  events: MinecraftTimelineEvent[];
}

/** The scrubber's event glyphs, merged when closer than the 44 px tap floor; a merged mark
 *  shows a death first, then a portal trip. */
export function scrubMarks(
  events: MinecraftTimelineEvent[],
  from: number,
  to: number,
  width: number,
  gap = 44,
): ScrubMark[] {
  const span = Math.max(1, to - from);
  const out: ScrubMark[] = [];
  for (const e of [...events].sort((a, b) => a.at - b.at)) {
    if (!SCRUB_KINDS.has(e.kind) || e.at < from || e.at > to) continue;
    const left = ((e.at - from) / span) * width;
    const c = out[out.length - 1];
    if (c && left - c.left < gap) c.events.push(e);
    else out.push({ left, lead: e, events: [e] });
  }
  for (const c of out) {
    c.lead =
      c.events.find((e) => e.kind === "death") ??
      c.events.find((e) => e.kind === "dimension") ??
      c.lead;
  }
  return out;
}

/** "minecraft:zombie_villager" → "Zombie Villager". */
export function entityName(typeId: string): string {
  return (typeId.split(":").pop() ?? typeId)
    .split("_")
    .filter(Boolean)
    .map((w) => (w[0] ?? "").toUpperCase() + w.slice(1))
    .join(" ");
}

const withArticle = (n: string) => `${/^[AEIOU]/.test(n) ? "an" : "a"} ${n}`;

/** A death in the game's own terms, from the damage cause the behavior pack logged and the
 *  killer's type: "burned in lava", "killed by a Zombie". */
export function deathCause(cause: string | null | undefined, killer?: string | null): string {
  const k = killer ? withArticle(entityName(killer)) : null;
  switch (cause) {
    case "lava":
      return "burned in lava";
    case "fire":
    case "fireTick":
    case "campfire":
    case "soulCampfire":
      return "burned to death";
    case "fall":
      return "fell from a high place";
    case "drowning":
      return "drowned";
    case "void":
      return "fell out of the world";
    case "suffocation":
      return "suffocated in a wall";
    case "starve":
      return "starved to death";
    case "freezing":
      return "froze to death";
    case "lightning":
      return "struck by lightning";
    case "magma":
      return "discovered the floor was lava";
    case "contact":
      return "pricked to death";
    case "flyIntoWall":
      return "flew into a wall";
    case "anvil":
    case "fallingBlock":
      return "squashed by a falling block";
    case "stalactite":
      return "skewered by a falling stalactite";
    case "stalagmite":
      return "impaled on a stalagmite";
    case "wither":
      return "withered away";
    case "sonicBoom":
      return "obliterated by a sonic boom";
    case "fireworks":
      return "went off with a bang";
    case "magic":
      return k ? `killed by ${k} using magic` : "killed by magic";
    case "projectile":
      return k ? `shot by ${k}` : "shot";
    case "entityExplosion":
    case "blockExplosion":
      return k ? `blown up by ${k}` : "blew up";
    default:
      return k ? `killed by ${k}` : "died";
  }
}

const DIM_SHORT: Record<MinecraftDim, string> = {
  overworld: "Overworld",
  nether: "Nether",
  the_end: "End",
};

/** What one event says, for the scrubber's labels and the tapped-item panel. */
export function eventWords(e: MinecraftTimelineEvent): string {
  const who = e.gamertag;
  if (e.kind === "death") return `${who} ${deathCause(e.cause, e.killer)}`;
  if (e.kind === "join") return `${who} logged in`;
  if (e.kind === "leave") return `${who} logged off`;
  if (e.kind === "respawn") return `${who} respawned`;
  return `${who} went to the ${DIM_SHORT[e.dim ?? "overworld"]}`;
}

/** The toast when a replay follows someone into another dimension. */
export function dimNote(gamertag: string, to: MinecraftDim, respawned: boolean): string {
  return respawned
    ? `${gamertag} died and respawned in the ${DIM_SHORT[to]}`
    : `${gamertag} went to the ${DIM_SHORT[to]}`;
}

const IN_DIM: Record<MinecraftDim, string> = {
  overworld: "the Overworld",
  nether: "the Nether",
  the_end: "the End",
};

const COMPASS = [
  "north",
  "north-east",
  "east",
  "south-east",
  "south",
  "south-west",
  "west",
  "north-west",
] as const;

/** The player card's Tricorder line: how far and which way, as the crow flies (north is
 *  -z, as in game), or which dimension the target is in when it's not theirs. */
export function tricorderLine(
  p: { dim: MinecraftDim; x: number; z: number },
  t: MinecraftTricorderTarget,
): string {
  if (t.dim !== p.dim) return `${t.label} · in ${IN_DIM[t.dim]}`;
  const dx = t.x + 0.5 - p.x;
  const dz = t.z + 0.5 - p.z;
  const blocks = Math.round(Math.hypot(dx, dz));
  if (blocks < 1) return `${t.label} · here`;
  const turn = ((Math.atan2(dx, -dz) * 180) / Math.PI + 360) % 360;
  const way = COMPASS[Math.round(turn / 45) % 8];
  const depth = t.y === null ? "" : ` · y ${t.y}`;
  return `${t.label} · ${blocks} block${blocks === 1 ? "" : "s"} ${way}${depth}`;
}

// ---- the Tricorder's live scan (§M25; DESIGN "The Tricorder's live scan on the map") ----

/** The pack's SCAN_REACH: a 33 × 33 × 33 box round the player. */
export const SCAN_REACH = 16;
/** Each dimension's lowest and highest block, where the box is cut (never shifted). */
const HEIGHT: Record<MinecraftDim, [number, number]> = {
  overworld: [-64, 319],
  nether: [0, 127],
  the_end: [0, 255],
};

/** The heights one scan reads for a player at `y`, as the pack's `scanBox` cuts them. */
export function scanYRange(dim: MinecraftDim, y: number): [number, number] {
  const [lo, hi] = HEIGHT[dim];
  const at = Math.floor(y);
  return [clamp(at - SCAN_REACH, lo, hi), clamp(at + SCAN_REACH, lo, hi)];
}

/** "40 s ago", "3 min ago", "2 h ago". */
export function agoWords(sec: number, now: number): string {
  const s = Math.max(0, Math.round(now - sec));
  if (s < 60) return `${s}${NB}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}${NB}min ago`;
  return `${Math.floor(s / 3600)}${NB}h ago`;
}

/** The view that frames a tapped scanner's whole square, with room for its plate: the
 *  33 blocks at up to 4 px a block (the mock's camera). */
export function scanFrame(p: { x: number; z: number }, f: Frame): MapView | null {
  const bx = Math.floor(p.x);
  const bz = Math.floor(p.z);
  return fitView(
    [
      [bx - SCAN_REACH, bz - SCAN_REACH],
      [bx + SCAN_REACH + 1, bz + SCAN_REACH + 1],
    ],
    f,
    40,
    4,
  );
}

/** Whether a player's scan is drawn on the map: on, and they're online to run it. */
export const scanShown = (p: MinecraftMapPlayer) => !!p.scan && p.online;

/** The one plate a scanning player wears on the map: their name first, so a scanner never
 *  has fewer words than anyone else (the review's A1). */
export function scanPlate(p: MinecraftMapPlayer): string | null {
  const s = p.scan;
  if (!s || !p.online) return null;
  return `${p.gamertag} · Scanning: ${s.label}${s.state === "none" ? " · none nearby" : ""}`;
}

export interface ScanWords {
  /** After **Tricorder** ·, in the held Tricorder's own words. */
  line: string;
  /** How far below or above them the block is, which a top-down map can't show. */
  depth: string | null;
  /** The reach, when it last looked, and the waypoint waiting under it. */
  sub: string;
}

/** The player card's scan lines, or null with no scan on. The distance is `tricorderLine`'s
 *  (across, to the block's centre), so the card says what the held Tricorder says. */
export function scanWords(p: MinecraftMapPlayer, now: number): ScanWords | null {
  const s = p.scan;
  if (!s) return null;
  const label = `Scanning: ${s.label}`;
  if (!p.online)
    return { line: `${label} — resumes when ${p.gamertag} is back on`, depth: null, sub: "" };
  const [lo, hi] = scanYRange(p.dim, p.y);
  const reach = `${SCAN_REACH}${NB}blocks, y${NB}${lo} to ${hi}`;
  const when = s.at === null ? "" : agoWords(s.at, now);
  const way = p.target ? ` Their waypoint, ${p.target.label}, comes back when the scan stops.` : "";
  if (s.state === "found" && s.found) {
    const f = s.found;
    const line = tricorderLine(p, { ...f, label, set_at: s.at ?? 0 });
    let depth: string | null = null;
    if (f.dim === p.dim) {
      const dy = f.y - Math.floor(p.y);
      const n = Math.abs(dy);
      const words = `${n}${NB}block${n === 1 ? "" : "s"} ${dy < 0 ? "below" : "above"} them`;
      depth = `${dy === 0 ? "Level with them" : `${dy < 0 ? "↓" : "↑"} ${words}`} · y${NB}${f.y}`;
    }
    return { line, depth, sub: `Within ${reach}. Found ${when}.${way}` };
  }
  if (s.state === "none") {
    return {
      line: `${label} · none nearby`,
      depth: null,
      sub: `Nothing within ${reach}.${when ? ` Last scan ${when}.` : ""}${way}`,
    };
  }
  return { line: label, depth: null, sub: `Within ${reach}. The first scan is on its way.${way}` };
}
