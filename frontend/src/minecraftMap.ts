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
  MinecraftSlot,
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

export const tileKey = (
  slot: string,
  dim: MinecraftDim,
  t: { zoom: number; tx: number; tz: number },
) => `${slot}|${dim}|${t.zoom}|${t.tx}|${t.tz}`;

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

export type StatusTone = "ok" | "off" | "bad" | "spin";

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
