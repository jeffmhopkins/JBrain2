// The Minecraft map's picture, shared by the full map (MinecraftMap.tsx) and the chat card
// (agent/views/minecraftMapCard.tsx): fetching the box's map tiles, reading the map-only
// tokens, and drawing every marker, trail, label and the scale bar onto a canvas. One
// renderer, so the card and the screen it opens can never draw the same world differently
// (DESIGN "The Minecraft map", "`minecraft_map` tool-view").

import {
  MOCK_MODE,
  type MinecraftBeacon,
  type MinecraftDim,
  type MinecraftGate,
  type MinecraftMapPlayer,
  type MinecraftMob,
  type MinecraftPlace,
  type MinecraftSpawnPoint,
  type MinecraftStructure,
  type MinecraftTimelineEvent,
  type MinecraftVillager,
  minecraftTileUrl,
} from "../api/client";
import {
  type BaseLayer,
  type Cluster,
  type Frame,
  GLYPHS,
  type MapView,
  type Placed,
  SCAN_REACH,
  type TileSpot,
  initialOf,
  scaleBar,
  structureInfo,
  toScreen,
  toWorld,
} from "../minecraftMap";
import { REACH_BLOCKS, type VillageMark, aimedAtPlace, jobOf } from "../minecraftMapLayers";
import {
  type Avoid,
  type Box,
  NO_AVOID,
  beaconArea,
  beaconKey,
  blocked,
  elsewhereLabel,
  gateCenter,
  linesHere,
  segHitsBox,
  sideSlot,
} from "../minecraftMapMarkers";

/** A point marker that clusters: a structure, an event (a death; on the replay also a log-in,
 *  log-off or portal trip), the world spawn, a beacon or gate, and — from the layers round
 *  (GUI gate minecraft-map-layers) — a saved place, a village, a villager, a pet or a named
 *  mob, or a player's bed. `tok` is a player's colour token; null takes the neutral ring;
 *  a bed's `col` is its player's colour, resolved. */
export type Marker = Placed &
  (
    | { type: "struct"; s: MinecraftStructure }
    | { type: "event"; e: MinecraftTimelineEvent }
    | { type: "spawn" }
    | { type: "beacon"; b: MinecraftBeacon }
    | { type: "gate"; g: MinecraftGate }
    | { type: "place"; p: MinecraftPlace; tok: string | null; own: string }
    | { type: "village"; v: VillageMark }
    | { type: "villager"; v: MinecraftVillager; label: string | null }
    | { type: "pet"; m: MinecraftMob; tok: string | null }
    | { type: "named"; m: MinecraftMob }
    | { type: "bed"; s: MinecraftSpawnPoint; col: string }
  );

export type TileEntry = {
  img: CanvasImageSource | null;
  at: number;
  state: "ok" | "loading" | "failed";
};

export function loadImage(url: string): Promise<CanvasImageSource> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("tile"));
    img.src = url;
  });
}

export async function fetchTile(
  slot: string,
  dim: MinecraftDim,
  zoom: number,
  tx: number,
  tz: number,
  layer: BaseLayer = "surface",
  url: (
    slot: string,
    dim: MinecraftDim,
    zoom: number,
    tx: number,
    tz: number,
    layer: BaseLayer,
  ) => string = minecraftTileUrl,
): Promise<CanvasImageSource> {
  if (MOCK_MODE) {
    // The mock world has biomes, not blocks: both layers draw it the same.
    const m = await import("../minecraftMapFixtures");
    return m.mockTile(slot, dim, zoom, tx, tz);
  }
  return loadImage(url(slot, dim, zoom, tx, tz, layer));
}

export interface Tokens {
  voidc: string;
  fog: string;
  rose: string;
  halo: string;
  plate: string;
  ink: string;
  inkOn: string;
  text: string;
  amber: string;
  green: string;
  violet: string;
  border: string;
  s2: string;
  /** The page's computed style, to read a player's colour token by name. */
  cs: CSSStyleDeclaration;
}

export function readTokens(el: Element): Tokens {
  const cs = getComputedStyle(el);
  const g = (n: string) => cs.getPropertyValue(n).trim();
  return {
    voidc: g("--map-void"),
    fog: g("--map-fog"),
    rose: g("--rose"),
    halo: g("--map-halo"),
    plate: g("--map-plate"),
    ink: g("--map-ink"),
    inkOn: g("--map-ink-on"),
    text: g("--text"),
    amber: g("--amber"),
    green: g("--green"),
    violet: g("--violet"),
    border: g("--border"),
    s2: g("--surface-2"),
    cs,
  };
}

export const colorOf = (t: Tokens, token: string) => t.cs.getPropertyValue(token).trim() || t.ink;

export const reducedMotion = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

const PATHS = new Map<string, Path2D>();
export function glyphPath(key: keyof typeof GLYPHS): Path2D | null {
  if (typeof Path2D === "undefined") return null;
  let p = PATHS.get(key);
  if (!p) {
    p = new Path2D(GLYPHS[key]);
    PATHS.set(key, p);
  }
  return p;
}

export interface Label {
  txt: string;
  x: number;
  y: number;
  pr: number;
  left?: boolean;
  /** What the label belongs to: it may touch its own marker and line, nothing else's. */
  own?: string | undefined;
  /** Placed by the side rule (right, left, below, above, else dropped) round a marker of
   *  this radius, rather than above it (§M26). */
  side?: number | undefined;
  /** Tried in its place when `txt` would be dropped: a scanning player's plate falls back
   *  to their name, so it is never dropped where a plain name would fit (§M25). A tapped
   *  thing's label (pr < 0) is never dropped, so it keeps its whole text. */
  short?: string | undefined;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export const FONT = "500 12px system-ui, sans-serif";

export function stars(ctx: CanvasRenderingContext2D, f: Frame, t: Tokens) {
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

/** Draws the base layer's tiles and returns how many are still on their way. A Surface tile
 *  the box hasn't drawn yet (a true-colour tile costs it up to a second once the world moves)
 *  shows the Biomes tile in its place, asked for first because the box draws it in
 *  milliseconds, so the map is never a field of holes while Surface catches up. `peek` reads
 *  the cache without asking the box; `want` asks if the tile isn't there. */
export function drawTiles(
  ctx: CanvasRenderingContext2D,
  spots: TileSpot[],
  layer: BaseLayer,
  peek: (s: TileSpot, layer: BaseLayer) => TileEntry | undefined,
  want: (s: TileSpot, layer: BaseLayer) => TileEntry | null,
  t: Tokens,
): number {
  if (layer === "surface") {
    for (const s of spots) if (!peek(s, "surface")?.img) want(s, "biomes");
  }
  let pending = 0;
  for (const s of spots) {
    const top = want(s, layer);
    const shown = top?.img ? top : layer === "surface" ? peek(s, "biomes") : undefined;
    if (!top?.img && top?.state !== "failed") pending++;
    if (shown?.img) {
      ctx.drawImage(shown.img, 0, 0, 256, 256, s.sx, s.sy, s.size + 0.5, s.size + 0.5);
    } else {
      hole(ctx, s.sx, s.sy, s.size, top?.state === "failed" ? "failed" : "loading", t);
    }
  }
  return pending;
}

/** A tile not here yet: shimmer-hatched while it loads, finely hatched if it failed. */
export function hole(
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

export function grid(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  t: Tokens,
  labels: Label[],
) {
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

export function structGlyph(
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

/** The structures' tile — a dark halo, a 1.5 px accent ring (dashed for a dormant gate) —
 *  with an ink glyph; a part named in `fill` is filled with the accent first. */
function tileGlyph(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  accent: string | null,
  parts: (keyof typeof GLYPHS)[],
  t: Tokens,
  opts: { dashed?: boolean; fill?: keyof typeof GLYPHS | null } = {},
) {
  ctx.save();
  ctx.fillStyle = t.halo;
  ctx.beginPath();
  ctx.roundRect?.(x - 12, y - 12, 24, 24, 7);
  ctx.fill();
  neutralOr(ctx, accent, t);
  ctx.lineWidth = 1.5;
  if (opts.dashed) ctx.setLineDash([3, 2.5]);
  ctx.beginPath();
  ctx.roundRect?.(x - 10.5, y - 10.5, 21, 21, 6);
  ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);
  ctx.translate(x - 7.5, y - 7.5);
  ctx.scale(15 / 24, 15 / 24);
  ctx.strokeStyle = t.ink;
  ctx.lineWidth = 2.2;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  for (const key of parts) {
    const path = glyphPath(key);
    if (!path) continue;
    if (opts.fill === key && accent) {
      ctx.fillStyle = accent;
      ctx.fill(path);
    }
    ctx.stroke(path);
  }
  ctx.restore();
}

/** A ring in a player's colour, or nobody's: `--map-ink` at 55 % (a villager, a named
 *  mob, a village with no located structure, a pet whose owner isn't known). */
function neutralOr(ctx: CanvasRenderingContext2D, col: string | null, t: Tokens) {
  if (col) {
    ctx.strokeStyle = col;
  } else {
    ctx.strokeStyle = t.ink;
    ctx.globalAlpha = 0.55;
  }
}

/** A Lucide glyph in `--map-ink`, `size` px square, centred on (x, y). */
function inkGlyph(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  key: keyof typeof GLYPHS,
  size: number,
  t: Tokens,
) {
  const path = glyphPath(key);
  if (!path) return;
  ctx.save();
  ctx.translate(x - size / 2, y - size / 2);
  ctx.scale(size / 24, size / 24);
  ctx.strokeStyle = t.ink;
  ctx.lineWidth = (2.2 * 15) / size;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.stroke(path);
  ctx.restore();
}

/** A round halo disc with a 1.5 px ring: a saved place's pin, a villager's job. */
function haloDisc(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  r: number,
  col: string | null,
  t: Tokens,
) {
  ctx.save();
  ctx.fillStyle = t.halo;
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.fill();
  neutralOr(ctx, col, t);
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.arc(x, y, r - 1.5, 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
}

/** A saved place: the pin on a halo disc ringed in its player's colour. Round, because a
 *  place is somewhere someone named, not a thing built in the world (those are tiles). */
export function savedPin(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  col: string | null,
  t: Tokens,
  on: boolean,
) {
  haloDisc(ctx, x, y, 12, col, t);
  inkGlyph(ctx, x, y, "pin", 15, t);
  if (on) ring(ctx, x, y, 18, t);
}

/** A villager: its job's glyph on a neutral disc (nobody's). */
export function villagerDisc(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  job: string,
  t: Tokens,
  on: boolean,
) {
  haloDisc(ctx, x, y, 11, null, t);
  inkGlyph(ctx, x, y, jobOf(job).glyph, 12, t);
  if (on) ring(ctx, x, y, 17, t);
}

/** Where a village tile's count goes: the first corner no line crosses (the review's A1 —
 *  never on a gate's dotted line), else the top right. */
export function cornerBox(x: number, y: number, w: number, segs: Avoid["segs"]): Box {
  const slots: Box[] = [
    { x: x + 6, y: y - 20, w, h: 16 },
    { x: x - 6 - w, y: y - 20, w, h: 16 },
    { x: x + 6, y: y + 4, w, h: 16 },
    { x: x - 6 - w, y: y + 4, w, h: 16 },
  ];
  return slots.find((r) => !segs.some((sg) => segHitsBox(sg, r))) ?? (slots[0] as Box);
}

/** A village: the house on an amber tile where the server located one, a neutral one
 *  where it didn't, with how many it stands for on a corner. Returns the count's box, a
 *  label obstacle. */
export function villageTile(
  ctx: CanvasRenderingContext2D,
  v: VillageMark,
  x: number,
  y: number,
  t: Tokens,
  segs: Avoid["segs"],
): Box {
  tileGlyph(ctx, x, y, v.struct ? t.amber : null, ["house"], t);
  const txt = String(v.count);
  ctx.save();
  ctx.font = "700 10.5px system-ui, sans-serif";
  const w = Math.max(18, ctx.measureText(txt).width + 9);
  const r = cornerBox(x, y, w, segs);
  ctx.fillStyle = t.halo;
  ctx.beginPath();
  ctx.roundRect?.(r.x, r.y, w, 16, 8);
  ctx.fill();
  ctx.strokeStyle = t.ink;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect?.(r.x + 0.5, r.y + 0.5, w - 1, 15, 7.5);
  ctx.stroke();
  ctx.fillStyle = t.ink;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(txt, r.x + w / 2, r.y + 8.5);
  ctx.restore();
  return r;
}

/** A pet: the paw on a tile ringed in its owner's colour; a named mob: the tag on a
 *  neutral one. Square, as things in the world are. */
export function mobTile(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  pet: boolean,
  col: string | null,
  t: Tokens,
  on: boolean,
) {
  tileGlyph(ctx, x, y, pet ? col : null, [pet ? "paw" : "tag"], t);
  if (on) ring(ctx, x, y, 18, t);
}

/** The 48-block reach round a tapped place, where goals count its chests (§M24): a
 *  hairline in its player's colour over a halo, with a 10 % wash. Round, because the reach
 *  is measured across — never a beacon's dashed square or the scan's corners. */
export function reachCircle(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  p: { x: number; z: number },
  col: string | null,
  t: Tokens,
) {
  const [x, y] = toScreen(v, f, p.x + 0.5, p.z + 0.5);
  const r = REACH_BLOCKS * v.s;
  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, r, 0, Math.PI * 2);
  ctx.globalAlpha = 0.1;
  ctx.fillStyle = col ?? t.ink;
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 4;
  ctx.stroke();
  neutralOr(ctx, col, t);
  ctx.lineWidth = 1.5;
  ctx.stroke();
  ctx.restore();
}

/** A No-Spawn Beacon: the shield on a green-ringed tile (green is *safe*). */
export function beaconGlyph(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  t: Tokens,
  on: boolean,
) {
  tileGlyph(ctx, x, y, t.green, ["shield"], t);
  if (on) ring(ctx, x, y, 18, t);
}

/** A wormhole gate: the portal on a violet-ringed tile, filled while open; dormant, the
 *  same portal and swirl unfilled in a dashed ring, so it still reads as a gate. */
export function gateGlyph(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  open: boolean,
  t: Tokens,
  on: boolean,
) {
  tileGlyph(ctx, x, y, t.violet, ["portalFrame", "swirl"], t, {
    dashed: !open,
    fill: open ? "portalFrame" : null,
  });
  if (on) ring(ctx, x, y, 18, t);
}

/** A player's respawn point: Lucide `bed` (or `anchor`) on a tile ringed in their colour;
 *  dashed when no bed or anchor was seen at the point (review X4: the game keeps a point
 *  after its bed is broken), M26's language for "not active". */
export function bedGlyph(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  kind: string,
  col: string,
  t: Tokens,
  on: boolean,
) {
  tileGlyph(ctx, x, y, col, [kind === "anchor" ? "anchor" : "bed"], t, {
    dashed: kind === "other",
  });
  if (on) ring(ctx, x, y, 18, t);
}

/** "Respawned here": a curve from a death to where its player woke, 2 px in their colour
 *  over a halo, with an arrowhead at the waking. Curved because it is a jump, not a path:
 *  never a trail (solid, straight), the bearing (dashed) or a gate line (dotted). Returns
 *  the curve as short segments, for labels to keep off; none when the ends touch. */
export function respawnCurve(
  ctx: CanvasRenderingContext2D,
  ax: number,
  ay: number,
  bx: number,
  by: number,
  col: string,
  t: Tokens,
): { ax: number; ay: number; bx: number; by: number }[] {
  const len = Math.hypot(bx - ax, by - ay);
  if (len < 40) return [];
  const ux = (bx - ax) / len;
  const uy = (by - ay) / len;
  const mx = (ax + bx) / 2 - uy * len * 0.22;
  const my = (ay + by) / 2 + ux * len * 0.22;
  const t0 = 14 / len;
  const t1 = 1 - 16 / len;
  const q = (s: number): [number, number] => [
    (1 - s) ** 2 * ax + 2 * (1 - s) * s * mx + s * s * bx,
    (1 - s) ** 2 * ay + 2 * (1 - s) * s * my + s * s * by,
  ];
  const pts: [number, number][] = [];
  for (let i = 0; i <= 40; i++) pts.push(q(t0 + ((t1 - t0) * i) / 40));
  const path = () => {
    ctx.beginPath();
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  };
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  path();
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 5;
  ctx.stroke();
  path();
  ctx.strokeStyle = col;
  ctx.lineWidth = 2;
  ctx.stroke();
  const [ex, ey] = pts[pts.length - 1] ?? [bx, by];
  const [px, py] = pts[pts.length - 3] ?? [ax, ay];
  const l = Math.hypot(ex - px, ey - py) || 1;
  const vx = (ex - px) / l;
  const vy = (ey - py) / l;
  ctx.beginPath();
  ctx.moveTo(ex - vx * 8 - vy * 5, ey - vy * 8 + vx * 5);
  ctx.lineTo(ex, ey);
  ctx.lineTo(ex - vx * 8 + vy * 5, ey - vy * 8 - vx * 5);
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 5;
  ctx.stroke();
  ctx.strokeStyle = col;
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.restore();
  const segs: { ax: number; ay: number; bx: number; by: number }[] = [];
  for (let i = 0; i + 5 < pts.length; i += 5) {
    const a = pts[i];
    const b = pts[i + 5];
    if (a && b) segs.push({ ax: a[0], ay: a[1], bx: b[0], by: b[1] });
  }
  return segs;
}

/** A beacon's protected square, drawn to the block: a green dash over a halo, filled when
 *  it's the tapped one so it reads apart from an overlapping neighbour. */
export function protectedArea(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  b: MinecraftBeacon,
  filled: boolean,
  t: Tokens,
) {
  const a = beaconArea(b);
  const [x0, y0] = toScreen(v, f, a.x0, a.z0);
  const [x1, y1] = toScreen(v, f, a.x1, a.z1);
  ctx.save();
  if (filled) {
    ctx.globalAlpha = 0.24;
    ctx.fillStyle = t.green;
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    ctx.globalAlpha = 1;
  }
  ctx.setLineDash([6, 4]);
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 4;
  ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
  ctx.strokeStyle = t.green;
  ctx.lineWidth = 2;
  ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
  ctx.restore();
}

/** Where an open gate leads: a dotted violet line (1 on, 6 off, over a halo) with an
 *  arrowhead, from the gate's tile to its destination pin. Dotted, so it never reads as a
 *  solid trail or the Tricorder's dashed bearing. Returns the line as drawn, for labels. */
export function gateLink(
  ctx: CanvasRenderingContext2D,
  ax: number,
  ay: number,
  bx: number,
  by: number,
  t: Tokens,
): { ax: number; ay: number; bx: number; by: number } | null {
  const len = Math.hypot(bx - ax, by - ay);
  if (len < 34) return null; // the tiles touch: there is no line to draw
  const ux = (bx - ax) / len;
  const uy = (by - ay) / len;
  const sx = ax + ux * 15;
  const sy = ay + uy * 15;
  const ex = bx - ux * 15;
  const ey = by - uy * 15;
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.setLineDash([1, 6]);
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(ex, ey);
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 6;
  ctx.stroke();
  ctx.strokeStyle = t.violet;
  ctx.lineWidth = 3;
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(ex - ux * 9 - uy * 6, ey - uy * 9 + ux * 6);
  ctx.lineTo(ex, ey);
  ctx.lineTo(ex - ux * 9 + uy * 6, ey - uy * 9 - ux * 6);
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 6;
  ctx.stroke();
  ctx.strokeStyle = t.violet;
  ctx.lineWidth = 2.5;
  ctx.stroke();
  ctx.restore();
  return { ax: sx, ay: sy, bx: ex, by: ey };
}

/** The place an open gate leads to: a pin on a violet-ringed halo disc. */
export function placePin(ctx: CanvasRenderingContext2D, x: number, y: number, t: Tokens) {
  ctx.save();
  ctx.fillStyle = t.halo;
  ctx.beginPath();
  ctx.arc(x, y, 12, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = t.violet;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.arc(x, y, 10.5, 0, Math.PI * 2);
  ctx.stroke();
  const path = glyphPath("pin");
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
}

export interface WorldMarkersIn {
  beacons: MinecraftBeacon[];
  gates: MinecraftGate[];
  dim: MinecraftDim;
  showBeacons: boolean;
  showGates: boolean;
  /** The tapped beacon's key or gate's id, if one is. */
  selBeacon: string | null;
  selGate: number | null;
  /** Where a saved place's pin stands (`spotKey`): a gate aimed at that block ends at the
   *  place's own pin instead of its violet one (the review's X1, matched by block). */
  placedAt?: Set<string> | undefined;
}

export interface WorldMarkersOut {
  /** The tiles to cluster with the other point markers (the tapped one apart). */
  markers: Marker[];
  tapped: Marker | null;
  /** Each drawn line and destination pin: label obstacles, and the pins' taps. */
  avoid: Avoid;
  pins: { sx: number; sy: number; g: MinecraftGate }[];
  labels: Label[];
}

/** The parts of the beacons and gates that sit under every tile, in the binding order —
 *  protected areas, then gate lines, then the places they lead to — and the tiles
 *  themselves, handed back to cluster with the other point markers. The caller draws the
 *  tiles, then the players on top of everything (README "Draw order"). */
export function drawWorldMarkers(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  t: Tokens,
  w: WorldMarkersIn,
): WorldMarkersOut {
  const off = (sx: number, sy: number) => sx < -30 || sy < -30 || sx > f.w + 30 || sy > f.h + 30;
  const out: WorldMarkersOut = {
    markers: [],
    tapped: null,
    avoid: { rects: [], segs: [] },
    pins: [],
    labels: [],
  };
  if (w.showBeacons) {
    const here = w.beacons.filter((b) => b.dim === w.dim);
    // The tapped square last, so its fill sits over a neighbour's dash.
    for (const b of here) if (beaconKey(b) !== w.selBeacon) protectedArea(ctx, v, f, b, false, t);
    for (const b of here) if (beaconKey(b) === w.selBeacon) protectedArea(ctx, v, f, b, true, t);
    for (const b of here) {
      const [sx, sy] = toScreen(v, f, b.x + 0.5, b.z + 0.5);
      const m: Marker = { type: "beacon", b, x: b.x, z: b.z, sx, sy, pr: 3 };
      if (beaconKey(b) === w.selBeacon) out.tapped = m;
      else if (!off(sx, sy)) out.markers.push(m);
    }
  }
  if (w.showGates) {
    const here = w.gates.filter((g) => g.dim === w.dim);
    const ends: { g: MinecraftGate; sx: number; sy: number }[] = [];
    for (const g of here) {
      if (!linesHere(g, w.dim) || !g.target) continue;
      const [cx, cz] = gateCenter(g);
      const [ax, ay] = toScreen(v, f, cx, cz);
      const [bx, by] = toScreen(v, f, g.target.x + 0.5, g.target.z + 0.5);
      const seg = gateLink(ctx, ax, ay, bx, by, t);
      if (seg) out.avoid.segs.push({ ...seg, own: `g:${g.id}` });
      ends.push({ g, sx: bx, sy: by });
    }
    for (const e of ends) {
      const tg = e.g.target;
      if (tg && w.placedAt && aimedAtPlace(w.placedAt, tg)) continue;
      if (off(e.sx, e.sy)) continue;
      placePin(ctx, e.sx, e.sy, t);
      const own = `g:${e.g.id}`;
      out.avoid.rects.push({ x: e.sx - 12, y: e.sy - 12, w: 24, h: 24, own });
      out.pins.push({ sx: e.sx, sy: e.sy, g: e.g });
      out.labels.push({
        txt: e.g.target?.place ?? "",
        x: e.sx,
        y: e.sy,
        pr: e.g.id === w.selGate ? -1 : 2,
        own,
        side: 12,
      });
    }
    for (const g of here) {
      const [cx, cz] = gateCenter(g);
      const [sx, sy] = toScreen(v, f, cx, cz);
      const m: Marker = { type: "gate", g, x: cx, z: cz, sx, sy, pr: 2 };
      if (g.id === w.selGate) out.tapped = m;
      else if (!off(sx, sy)) out.markers.push(m);
      if (g.state === "open" && g.target && !linesHere(g, w.dim) && !off(sx, sy)) {
        out.labels.push({
          txt: elsewhereLabel(g),
          x: sx,
          y: sy,
          pr: g.id === w.selGate ? -1 : 2,
          own: `g:${g.id}`,
          side: 12,
        });
      }
    }
  }
  return out;
}

export function ring(ctx: CanvasRenderingContext2D, x: number, y: number, r: number, t: Tokens) {
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

/** A count badge; rose-ringed when everything in it is a death. */
export function clusterBadge(ctx: CanvasRenderingContext2D, c: Cluster<Marker>, t: Tokens) {
  ctx.save();
  ctx.beginPath();
  ctx.arc(c.sx, c.sy, 16, 0, Math.PI * 2);
  ctx.fillStyle = t.halo;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(c.sx, c.sy, 13, 0, Math.PI * 2);
  ctx.lineWidth = 2;
  ctx.strokeStyle = c.members.every((m) => m.type === "event" && m.e.kind === "death")
    ? t.rose
    : t.ink;
  ctx.stroke();
  ctx.fillStyle = t.ink;
  ctx.font = "700 12px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(String(c.members.length), c.sx, c.sy + 0.5);
  ctx.restore();
}

export function playerDisc(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  p: MinecraftMapPlayer,
  col: string,
  t: Tokens,
  on: boolean,
  pulse: boolean,
) {
  ctx.save();
  if (pulse) {
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

export function markerLabel(m: Marker): string | null {
  if (m.type === "struct") return structureInfo(m.s.structure).label;
  if (m.type === "spawn") return "Spawn";
  if (m.type === "place") return m.p.name;
  if (m.type === "village") return m.v.label;
  if (m.type === "villager") return m.label;
  if (m.type === "pet" || m.type === "named") return m.m.name || null;
  if (m.type === "bed") {
    return m.s.kind === "anchor" ? "Respawn anchor" : m.s.kind === "other" ? "Spawn point" : "Bed";
  }
  return null;
}

/** A marker's key for the label rule: its label, line and pin share it, and every other
 *  label keeps off it. Keyed per marker, never per kind, so one pet's plate can't cover
 *  another pet (the review's X5). */
export function markerOwn(m: Marker): string | undefined {
  if (m.type === "gate") return `g:${m.g.id}`;
  if (m.type === "beacon") return `b:${beaconKey(m.b)}`;
  if (m.type === "place") return m.own;
  if (m.type === "village") return `vg:${m.v.id}`;
  if (m.type === "villager") return `vv:${m.v.id}`;
  if (m.type === "pet") return `pet:${m.m.id}`;
  if (m.type === "named") return `nm:${m.m.id}`;
  if (m.type === "bed") return `bed:${m.s.xuid}`;
  return undefined;
}

/** The colour a marker's ring takes, from its player's token; null is nobody's. */
const ringOf = (t: Tokens, tok: string | null) => (tok ? colorOf(t, tok) : null);

export function markerGlyph(ctx: CanvasRenderingContext2D, m: Marker, t: Tokens, on: boolean) {
  if (m.type === "struct") return structGlyph(ctx, m.sx, m.sy, m.s.structure, t, on);
  if (m.type === "beacon") return beaconGlyph(ctx, m.sx, m.sy, t, on);
  if (m.type === "gate") return gateGlyph(ctx, m.sx, m.sy, m.g.state === "open", t, on);
  if (m.type === "place") return savedPin(ctx, m.sx, m.sy, ringOf(t, m.tok), t, on);
  if (m.type === "villager") return villagerDisc(ctx, m.sx, m.sy, m.v.job, t, on);
  if (m.type === "pet") return mobTile(ctx, m.sx, m.sy, true, ringOf(t, m.tok), t, on);
  if (m.type === "named") return mobTile(ctx, m.sx, m.sy, false, null, t, on);
  if (m.type === "village") {
    villageTile(ctx, m.v, m.sx, m.sy, t, []);
    if (on) ring(ctx, m.sx, m.sy, 18, t);
    return;
  }
  if (m.type === "bed") return bedGlyph(ctx, m.sx, m.sy, m.s.kind, m.col, t, on);
  const { sx: x, sy: y } = m;
  ctx.save();
  if (m.type === "event" && m.e.kind !== "death") {
    // A log-in, log-out or portal: the scrubber's Lucide glyph on the dark halo.
    ctx.beginPath();
    ctx.arc(x, y, on ? 13 : 11, 0, Math.PI * 2);
    ctx.fillStyle = t.halo;
    ctx.fill();
    const path = glyphPath(
      m.e.kind === "join" ? "login" : m.e.kind === "leave" ? "logout" : "portal",
    );
    if (path) {
      ctx.translate(x - 7, y - 7);
      ctx.scale(14 / 24, 14 / 24);
      ctx.strokeStyle = t.ink;
      ctx.lineWidth = 2.2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke(path);
    }
  } else if (m.type === "event") {
    // A skull: a rose disc with the glyph's two eyes and teeth in the dark ink.
    const r = on ? 11 : 9;
    ctx.beginPath();
    ctx.arc(x, y, r + 2, 0, Math.PI * 2);
    ctx.fillStyle = t.halo;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = t.rose;
    ctx.fill();
    ctx.fillStyle = t.inkOn;
    ctx.beginPath();
    ctx.arc(x - 3, y - 1, 2, 0, Math.PI * 2);
    ctx.arc(x + 3, y - 1, 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillRect(x - 2.5, y + 3, 1.5, 3);
    ctx.fillRect(x + 1, y + 3, 1.5, 3);
  } else {
    // The world spawn: a star.
    ctx.beginPath();
    ctx.arc(x, y, 11, 0, Math.PI * 2);
    ctx.fillStyle = t.halo;
    ctx.fill();
    ctx.fillStyle = t.ink;
    ctx.beginPath();
    for (let i = 0; i < 10; i++) {
      const a = -Math.PI / 2 + (i * Math.PI) / 5;
      const r = i % 2 ? 3.6 : 8;
      ctx.lineTo(x + Math.cos(a) * r, y + Math.sin(a) * r);
    }
    ctx.closePath();
    ctx.fill();
  }
  ctx.restore();
  if (on) ring(ctx, x, y, 17, t);
}

/** A trail: a halo under the player's colour; faint behind the moment, bold for the tail. */
export function line(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  pts: [number, number][],
  col: string,
  t: Tokens,
  kind: "full" | "faint" | "tail",
) {
  if (pts.length < 2) return;
  ctx.save();
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  pts.forEach(([x, z], i) => {
    const [sx, sy] = toScreen(v, f, x, z);
    if (i === 0) ctx.moveTo(sx, sy);
    else ctx.lineTo(sx, sy);
  });
  const thin = v.s < 0.2;
  const [halo, width, alpha] =
    kind === "tail"
      ? [6, 3.5, 1]
      : kind === "faint"
        ? [thin ? 3.5 : 5, thin ? 1.5 : 2.5, 0.45]
        : [thin ? 3.5 : 5, thin ? 1.5 : 2.5, 1];
  ctx.globalAlpha = kind === "faint" ? 0.35 : kind === "full" ? 0.55 : 1;
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = halo;
  ctx.stroke();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = col;
  ctx.lineWidth = width;
  ctx.stroke();
  ctx.restore();
}

/** A player's bearing to their Tricorder target: a dashed line from where they stand to
 *  it, in their colour over a halo, so it reads apart from the solid trails. */
export function bearingLine(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  from: [number, number],
  to: [number, number],
  col: string,
  t: Tokens,
) {
  const [ax, ay] = toScreen(v, f, from[0], from[1]);
  const [bx, by] = toScreen(v, f, to[0], to[1]);
  ctx.save();
  ctx.lineCap = "round";
  ctx.setLineDash([8, 6]);
  ctx.beginPath();
  ctx.moveTo(ax, ay);
  ctx.lineTo(bx, by);
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 5;
  ctx.stroke();
  ctx.strokeStyle = col;
  ctx.lineWidth = 2.5;
  ctx.stroke();
  ctx.restore();
}

/** A live scan's field: the 33 × 33 blocks the pack searches round the player, drawn to the
 *  block. Viewfinder corners in the player's colour over a halo, joined by a hairline at
 *  70 %, so it reads as a sensor's field and never as a beacon's dashed square or the chunk
 *  grid; a 10 % wash while it has found something. Returns the square on screen. */
export function scanSquare(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  at: { x: number; z: number },
  col: string,
  found: boolean,
  t: Tokens,
): Rect {
  const bx = Math.floor(at.x);
  const bz = Math.floor(at.z);
  const [x0, y0] = toScreen(v, f, bx - SCAN_REACH, bz - SCAN_REACH);
  const [x1, y1] = toScreen(v, f, bx + SCAN_REACH + 1, bz + SCAN_REACH + 1);
  const w = x1 - x0;
  const h = y1 - y0;
  const c = Math.min(14, w / 4);
  ctx.save();
  if (found) {
    ctx.globalAlpha = 0.1;
    ctx.fillStyle = col;
    ctx.fillRect(x0, y0, w, h);
    ctx.globalAlpha = 1;
  }
  ctx.strokeStyle = t.halo;
  ctx.lineWidth = 2.5;
  ctx.strokeRect(x0, y0, w, h);
  ctx.globalAlpha = 0.7;
  ctx.strokeStyle = col;
  ctx.lineWidth = 1;
  ctx.strokeRect(x0, y0, w, h);
  ctx.globalAlpha = 1;
  ctx.lineCap = "round";
  const corners: [number, number, number, number][] = [
    [x0, y0, 1, 1],
    [x1, y0, -1, 1],
    [x0, y1, 1, -1],
    [x1, y1, -1, -1],
  ];
  for (const [lw, stroke] of [
    [5, t.halo],
    [2.5, col],
  ] as const) {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = lw;
    ctx.beginPath();
    for (const [cx, cy, sx, sy] of corners) {
      ctx.moveTo(cx + sx * c, cy);
      ctx.lineTo(cx, cy);
      ctx.lineTo(cx, cy + sy * c);
    }
    ctx.stroke();
  }
  ctx.restore();
  return { x: x0, y: y0, w, h };
}

/** The Tricorder's target: two rings and a centre dot in the player's colour. */
export function targetMark(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  col: string,
  t: Tokens,
) {
  ctx.save();
  for (const r of [11, 5]) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.strokeStyle = t.halo;
    ctx.lineWidth = 5;
    ctx.stroke();
    ctx.strokeStyle = col;
    ctx.lineWidth = 2.5;
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.arc(x, y, 1.5, 0, Math.PI * 2);
  ctx.fillStyle = col;
  ctx.fill();
  ctx.restore();
}

/** Explored by: a veil over every chunk the chosen player (or anyone) hadn't come near by
 *  `until` — each visible chunk they had is cut out of it. */
export function veil(
  ctx: CanvasRenderingContext2D,
  v: MapView,
  f: Frame,
  t: Tokens,
  chunks: [number, number, number][],
  until: number,
) {
  const [x0, z0] = toWorld(v, f, 0, 0);
  const [x1, z1] = toWorld(v, f, f.w, f.h);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, f.w, f.h);
  for (const [cx, cz, seen] of chunks) {
    if (seen > until) continue;
    const bx = cx * 16;
    const bz = cz * 16;
    if (bx + 16 < x0 || bx > x1 || bz + 16 < z0 || bz > z1) continue;
    const [sx, sy] = toScreen(v, f, bx, bz);
    const side = 16 * v.s;
    // Drawn counter-clockwise so the non-zero rule cuts it out of the veil.
    ctx.moveTo(sx, sy);
    ctx.lineTo(sx, sy + side);
    ctx.lineTo(sx + side, sy + side);
    ctx.lineTo(sx + side, sy);
    ctx.closePath();
  }
  ctx.globalAlpha = 0.62;
  ctx.fillStyle = t.fog;
  ctx.fill("nonzero");
  ctx.restore();
}

export function spotMark(
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

export interface ScaleRect extends Rect {
  bx: number;
  by: number;
  bw: number;
  txt: string;
}

export function scaleRect(ctx: CanvasRenderingContext2D, s: number, f: Frame): ScaleRect {
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

export function drawScale(ctx: CanvasRenderingContext2D, r: ScaleRect, t: Tokens) {
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

/** The tapped thing's label first, then players, then structures; each clamped inside the
 *  visible map, moved below its marker if it collides, or dropped — never drawn under a
 *  control or another label. A `side` label (a gate's destination) goes right, left, below
 *  or above its marker, or is dropped. Every label also keeps off what `avoid` holds — the
 *  beacon and gate tiles, the player discs and the gates' lines — other than its own. */
export function placeLabels(
  ctx: CanvasRenderingContext2D,
  labels: Label[],
  keep: Rect[],
  f: Frame,
  t: Tokens,
  avoid: Avoid = NO_AVOID,
) {
  const placed: Box[] = [];
  const visH = f.h - f.bottom;
  ctx.save();
  ctx.font = FONT;
  const queue = [...labels].sort((a, b) => a.pr - b.pr);
  for (let i = 0; i < queue.length; i++) {
    const l = queue[i] as Label;
    if (!l.left && l.y > visH) continue; // its marker is under the panel
    // Dropped, a label with a shorter form tries that next, in the same place in line.
    const drop = () => {
      if (l.short) queue.splice(i + 1, 0, { ...l, txt: l.short, short: undefined });
    };

    const w = ctx.measureText(l.txt).width + 12;
    const h = 20;
    let r: Rect | null;
    if (l.side !== undefined) {
      r = sideSlot(
        { x: l.x, y: l.y, r: l.side, own: l.own },
        w,
        h,
        { w: f.w, top: 4, bottom: visH },
        keep,
        placed,
        avoid,
      );
      if (!r) {
        drop();
        continue;
      }
    } else {
      r = l.left ? { x: l.x, y: l.y, w, h } : { x: l.x - w / 2, y: l.y - 26, w, h };
      r.x = Math.min(Math.max(r.x, 4), f.w - w - 4);
      r.y = Math.min(Math.max(r.y, 4), visH - h - 4);
      if (blocked(r, l.own, keep, placed, avoid)) {
        const below = { ...r, y: l.y + 30 };
        if (!l.left && below.y + h < visH - 4 && !blocked(below, l.own, keep, placed, avoid))
          r = below;
        else if (l.pr >= 0) {
          drop();
          continue;
        }
      }
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

/** The renderer's biome colours, for the legend: data from inside the picture. */
export const LEGEND: Record<MinecraftDim, [string, [number, number, number]][]> = {
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

export const asSurvey = (c: [number, number, number]) => {
  const g = c[0] * 0.3 + c[1] * 0.59 + c[2] * 0.11;
  return c.map((v) => Math.round((g + (v - g) * 0.5) * 0.65)).join(",");
};
