// No-Spawn Beacons and wormhole gates on the Minecraft map (binding mock
// docs/mocks/minecraft-map-markers/a-always-drawn.html, variant A as revised; plan §M26,
// §M12, §M16). The pure half: the protected square, where a gate's line runs and what each
// one says, the "In this world" rows, and where a label may go. The canvas half is in
// screens/minecraftMapDraw.ts.

import type {
  MinecraftBeacon,
  MinecraftDim,
  MinecraftGate,
  MinecraftMapMarkers,
} from "./api/client";

export const CHUNK = 16;
/** A beacon covers its own chunk and one on each side: 48 × 48 blocks, floor to sky. */
export const REACH = 1;

export const chunkOf = (v: number) => Math.floor(v / CHUNK);

/** The protected square's edges in blocks: x0/z0 is its first block, x1/z1 one past its
 *  last, so the square draws to the block. */
export function beaconArea(b: { x: number; z: number }) {
  const cx = chunkOf(b.x);
  const cz = chunkOf(b.z);
  return {
    x0: (cx - REACH) * CHUNK,
    z0: (cz - REACH) * CHUNK,
    x1: (cx + REACH + 1) * CHUNK,
    z1: (cz + REACH + 1) * CHUNK,
  };
}

export const beaconKey = (b: MinecraftBeacon) => `${b.dim}@${b.x},${b.y},${b.z}`;

/** How many other beacons' squares share ground with this one's. */
export function overlapsOf(b: MinecraftBeacon, all: MinecraftBeacon[]): number {
  return all.filter(
    (o) =>
      o !== b &&
      o.dim === b.dim &&
      Math.abs(chunkOf(o.x) - chunkOf(b.x)) <= 2 * REACH &&
      Math.abs(chunkOf(o.z) - chunkOf(b.z)) <= 2 * REACH,
  ).length;
}

/** The middle of a gate's 4-wide ring, from its lowest corner and the way it runs. */
export function gateCenter(g: Pick<MinecraftGate, "x" | "z" | "axis">): [number, number] {
  return g.axis === "x" ? [g.x + 2, g.z + 0.5] : [g.x + 0.5, g.z + 2];
}

/** The block a gate is told by: the middle of its ring, where a player steps through. */
export function gateSpot(g: Pick<MinecraftGate, "x" | "z" | "axis">) {
  const [cx, cz] = gateCenter(g);
  return { x: Math.floor(cx), z: Math.floor(cz) };
}

/** An open gate whose line ends in this dimension: both ends here. A gate into another
 *  dimension gets a label instead — its line would end in another dimension's numbers. */
export const linesHere = (g: MinecraftGate, dim: MinecraftDim) =>
  g.dim === dim && g.state === "open" && !!g.target && g.target.dim === dim;

/** A real minus sign, as the game's coordinates read. */
export const num = (v: number) => (v < 0 ? `−${-v}` : String(v));

const COMPASS = [
  "north",
  "north-east",
  "east",
  "south-east",
  "south",
  "south-west",
  "west",
  "north-west",
];

/** The way and distance across from one point to another; north is −z, as in game. */
export function bearingTo(fx: number, fz: number, tx: number, tz: number) {
  const dx = tx - fx;
  const dz = tz - fz;
  const turn = ((Math.atan2(dx, -dz) * 180) / Math.PI + 360) % 360;
  return {
    way: COMPASS[Math.round(turn / 45) % 8] ?? "north",
    blocks: Math.round(Math.hypot(dx, dz)),
  };
}

const IN_DIM: Record<MinecraftDim, string> = {
  overworld: "in the Overworld",
  nether: "in the Nether",
  the_end: "in the End",
};

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** "Oct 11" with a no-break space, so the date never breaks across a line. */
export function shortDate(sec: number): string {
  const d = new Date(sec * 1000);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

/** What a tapped gate's panel says (README "Tapping a gate"). */
export function gateWords(g: MinecraftGate) {
  const whose = g.planter ? `${g.planter}'s` : "planter unknown";
  const planted = g.planted_at ? ` · planted ${shortDate(g.planted_at)}` : "";
  const aimed = g.state === "open" && g.aimed_at ? `, aimed ${shortDate(g.aimed_at)}` : "";
  const t = g.target;
  let lead: string | null = null;
  let show: string | null = null;
  if (g.state === "open" && t) {
    const at = `X ${num(t.x)} · Z ${num(t.z)}`;
    if (t.dim === g.dim) {
      const [cx, cz] = gateCenter(g);
      const { way, blocks } = bearingTo(cx, cz, t.x + 0.5, t.z + 0.5);
      lead = `${blocks} blocks ${way} · ${at}`;
      show = `Show ${t.place}`;
    } else {
      lead = `${IN_DIM[t.dim]} · ${at}`;
      show = t.dim === "nether" ? "Show it in the Nether" : `Show it ${IN_DIM[t.dim]}`;
    }
  }
  return {
    title: `Wormhole gate ${g.id}`,
    sub: `${whose}${planted}${aimed}`,
    lead,
    show,
    dormant:
      g.state === "open"
        ? null
        : `Not open yet. ${g.planter || "Its planter"} can ask Dave to open it to one of their saved places, standing within 4 blocks.`,
  };
}

/** What a tapped beacon's panel says (README "Tapping a beacon"). */
export function beaconWords(b: MinecraftBeacon, all: MinecraftBeacon[]) {
  const a = beaconArea(b);
  const n = overlapsOf(b, all);
  const by = b.placed_by ? `placed by ${b.placed_by}` : "placer not recorded";
  return {
    title: "No-Spawn Beacon",
    sub: `${by}${b.placed_at ? `, ${shortDate(b.placed_at)}` : ""}`,
    area: `X ${num(a.x0)} to ${num(a.x1 - 1)}, Z ${num(a.z0)} to ${num(a.z1 - 1)}`,
    overlaps: n ? ` · overlaps ${n} other beacon${n > 1 ? "s" : ""}` : "",
  };
}

/** Where an open gate into another dimension leads, as the label by the gate says it. */
export const elsewhereLabel = (g: MinecraftGate) =>
  g.target ? `to ${g.target.place} · ${IN_DIM[g.target.dim]}` : "";

/** The Layers line under each overlay. */
export function overlayLines(m: MinecraftMapMarkers | null) {
  const nb = m?.beacons.length ?? 0;
  const open = m?.gates.filter((g) => g.state === "open").length ?? 0;
  const dormant = (m?.gates.length ?? 0) - open;
  return {
    beacons: nb
      ? `${nb} in this world · each with its 48 × 48 protected area`
      : "none in this world yet",
    gates:
      open || dormant
        ? `${open} open · ${dormant} dormant · a dotted line to where each leads`
        : "none in this world yet",
  };
}

export type ListRow =
  | { kind: "beacon"; key: string; title: string; sub: string; b: MinecraftBeacon }
  | { kind: "gate"; key: string; title: string; sub: string; g: MinecraftGate };

const xz = (o: { x: number; z: number }) => `X ${num(o.x)} · Z ${num(o.z)}`;

/** The Layers tab's "In this world": every beacon, then every gate, in any dimension, so
 *  one up in a sky base or out of view can still be found. */
export function listRows(m: MinecraftMapMarkers | null): ListRow[] {
  if (!m) return [];
  const dimNote = (d: MinecraftDim) => (d === "overworld" ? "" : ` · ${IN_DIM[d]}`);
  return [
    ...m.beacons.map(
      (b): ListRow => ({
        kind: "beacon",
        key: `b:${beaconKey(b)}`,
        title: `No-Spawn Beacon${b.placed_by ? ` · ${b.placed_by}` : ""}`,
        sub: `${xz(b)}${b.y > 150 ? ` · up at Y ${b.y}` : ""}${dimNote(b.dim)}${
          b.placed_at ? ` · placed ${shortDate(b.placed_at)}` : ""
        }`,
        b,
      }),
    ),
    ...m.gates.map((g): ListRow => {
      const t = g.target;
      const state =
        g.state === "open" && t
          ? `open, to ${t.place}${t.dim !== g.dim ? ` (${t.dim === "nether" ? "Nether" : t.dim === "the_end" ? "End" : "Overworld"})` : ""}`
          : "dormant";
      return {
        kind: "gate",
        key: `g:${g.id}`,
        title: `Gate ${g.id}${g.planter ? ` · ${g.planter}` : ""}`,
        sub: `${state} · ${xz(gateSpot(g))}${dimNote(g.dim)}`,
        g,
      };
    }),
  ];
}

// ---- where a label may go ----

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Something a label must not touch. `own` is the thing the label belongs to: a gate's
 *  label may touch its own gate's line and pin, never another's. */
export interface Avoid {
  rects: (Box & { own?: string | undefined })[];
  segs: { ax: number; ay: number; bx: number; by: number; own?: string | undefined }[];
}

export const NO_AVOID: Avoid = { rects: [], segs: [] };

const overlap = (a: Box, b: Box) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** Whether a segment passes through a box (Liang–Barsky clipping). */
export function segHitsBox(s: { ax: number; ay: number; bx: number; by: number }, r: Box): boolean {
  const dx = s.bx - s.ax;
  const dy = s.by - s.ay;
  let t0 = 0;
  let t1 = 1;
  const edges: [number, number][] = [
    [-dx, s.ax - r.x],
    [dx, r.x + r.w - s.ax],
    [-dy, s.ay - r.y],
    [dy, r.y + r.h - s.ay],
  ];
  for (const [p, q] of edges) {
    if (p === 0) {
      if (q < 0) return false;
      continue;
    }
    const t = q / p;
    if (p < 0) {
      if (t > t1) return false;
      if (t > t0) t0 = t;
    } else {
      if (t < t0) return false;
      if (t < t1) t1 = t;
    }
  }
  return t0 <= t1;
}

/** Whether a label's plate at `r` would touch anything it must not: a kept-out control,
 *  another label, a marker tile or player disc, or a line, other than its own. */
export function blocked(r: Box, own: string | undefined, keep: Box[], placed: Box[], avoid: Avoid) {
  return (
    keep.some((q) => overlap(r, q)) ||
    placed.some((q) => overlap(r, q)) ||
    avoid.rects.some((q) => (own === undefined || q.own !== own) && overlap(r, q)) ||
    avoid.segs.some((s) => (own === undefined || s.own !== own) && segHitsBox(s, r))
  );
}

/** The side rule (README "Labels"): to the right of the marker, else the left, else below,
 *  else above — or nowhere, and the label is dropped. `r` is the marker's radius. */
export function sideSlot(
  at: { x: number; y: number; r: number; own?: string | undefined },
  w: number,
  h: number,
  bounds: { w: number; top: number; bottom: number },
  keep: Box[],
  placed: Box[],
  avoid: Avoid,
): Box | null {
  const gap = 4;
  const slots: Box[] = [
    { x: at.x + at.r + gap, y: at.y - h / 2, w, h },
    { x: at.x - at.r - gap - w, y: at.y - h / 2, w, h },
    { x: at.x - w / 2, y: at.y + at.r + gap, w, h },
    { x: at.x - w / 2, y: at.y - at.r - gap - h, w, h },
  ];
  return (
    slots.find(
      (r) =>
        r.x >= 4 &&
        r.x + r.w <= bounds.w - 4 &&
        r.y >= bounds.top &&
        r.y + r.h <= bounds.bottom - 4 &&
        !blocked(r, at.own, keep, placed, avoid),
    ) ?? null
  );
}
