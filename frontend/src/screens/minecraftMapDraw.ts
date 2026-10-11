// The Minecraft map's picture, shared by the full map (MinecraftMap.tsx) and the chat card
// (agent/views/minecraftMapCard.tsx): fetching the box's map tiles, reading the map-only
// tokens, and drawing every marker, trail, label and the scale bar onto a canvas. One
// renderer, so the card and the screen it opens can never draw the same world differently
// (DESIGN "The Minecraft map", "`minecraft_map` tool-view").

import {
  MOCK_MODE,
  type MinecraftDim,
  type MinecraftMapPlayer,
  type MinecraftStructure,
  type MinecraftTimelineEvent,
  minecraftTileUrl,
} from "../api/client";
import {
  type BaseLayer,
  type Cluster,
  type Frame,
  GLYPHS,
  type MapView,
  type Placed,
  type TileSpot,
  initialOf,
  scaleBar,
  structureInfo,
  toScreen,
  toWorld,
} from "../minecraftMap";

/** A point marker that clusters: a structure, an event (a death; on the replay also a log-in,
 *  log-off or portal trip) or the world spawn. */
export type Marker = Placed &
  (
    | { type: "struct"; s: MinecraftStructure }
    | { type: "event"; e: MinecraftTimelineEvent }
    | { type: "spawn" }
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
): Promise<CanvasImageSource> {
  if (MOCK_MODE) {
    // The mock world has biomes, not blocks: both layers draw it the same.
    const m = await import("../minecraftMapFixtures");
    return m.mockTile(slot, dim, zoom, tx, tz);
  }
  return loadImage(minecraftTileUrl(slot, dim, zoom, tx, tz, layer));
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
  return null;
}

export function markerGlyph(ctx: CanvasRenderingContext2D, m: Marker, t: Tokens, on: boolean) {
  if (m.type === "struct") return structGlyph(ctx, m.sx, m.sy, m.s.structure, t, on);
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
 *  control or another label. */
export function placeLabels(
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
