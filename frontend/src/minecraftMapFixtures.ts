// dev:mock's Minecraft map: the reviewed mock's world (docs/mocks/minecraft-map/README.md
// "Shared fixture") drawn procedurally, so the map screen pans over something real-looking
// with no box. Tiles are painted here the way the renderer paints them — the biome table,
// hillshade, and the satellite survey half-desaturated at 65% outside explored ground —
// and the routes answer from the same world. Only ever imported in mock mode.

import type {
  MinecraftDim,
  MinecraftMapInfo,
  MinecraftMapPlayers,
  MinecraftMapPoint,
  MinecraftMapStructures,
} from "./api/client";

const nowS = () => Date.now() / 1000;

// The renderer's own biome colours (deploy/minecraft/mapping.py BIOME_COLORS): data inside
// the picture, not UI chrome.
const BIOMES: [string, [number, number, number]][] = [
  ["Ocean", [0, 0, 112]],
  ["Deep Ocean", [0, 0, 48]],
  ["River", [0, 0, 255]],
  ["Beach", [250, 222, 85]],
  ["Plains", [141, 179, 96]],
  ["Forest", [5, 102, 33]],
  ["Birch Forest", [48, 116, 68]],
  ["Dark Forest", [64, 81, 26]],
  ["Taiga", [11, 102, 89]],
  ["Snowy Plains", [255, 255, 255]],
  ["Desert", [250, 148, 24]],
  ["Savanna", [189, 178, 95]],
  ["Cherry Grove", [255, 145, 200]],
  ["Meadow", [131, 187, 109]],
  ["Nether Wastes", [191, 59, 59]],
  ["Crimson Forest", [221, 8, 8]],
  ["Warped Forest", [73, 144, 123]],
  ["Soul Sand Valley", [94, 56, 48]],
  ["Basalt Deltas", [64, 54, 54]],
  ["The End", [128, 128, 255]],
];
const WATER = new Set([0, 1, 2]);

function noise(seed: number) {
  const h = (x: number, z: number) => {
    let n =
      Math.imul(x | 0, 374761393) ^ Math.imul(z | 0, 668265263) ^ Math.imul(seed | 0, 1442695041);
    n = Math.imul(n ^ (n >>> 13), 1274126177);
    n ^= n >>> 16;
    return (n >>> 0) / 4294967296;
  };
  const sm = (t: number) => t * t * (3 - 2 * t);
  const vn = (x: number, z: number) => {
    const xi = Math.floor(x);
    const zi = Math.floor(z);
    const u = sm(x - xi);
    const v = sm(z - zi);
    const a = h(xi, zi);
    const b = h(xi + 1, zi);
    const c = h(xi, zi + 1);
    const d = h(xi + 1, zi + 1);
    return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
  };
  return (x: number, z: number, octaves = 4) => {
    let s = 0;
    let amp = 0.5;
    let f = 1;
    let n = 0;
    for (let i = 0; i < octaves; i++) {
      s += amp * vn(x * f + i * 17.31, z * f - i * 9.17);
      n += amp;
      amp *= 0.5;
      f *= 2.03;
    }
    return s / n;
  };
}

const elev = noise(11);
const temp = noise(23);
const wet = noise(37);
const neth = noise(41);
const isle = noise(53);

/** [biome index, height] at a column, or null for the End's void. */
function sample(dim: MinecraftDim, x: number, z: number): [number, number] | null {
  if (dim === "nether") {
    const n = neth(x / 220, z / 220);
    const b = n < 0.38 ? 17 : n < 0.47 ? 14 : n < 0.55 ? 15 : n < 0.64 ? 16 : 18;
    return [b, 128];
  }
  if (dim === "the_end") {
    const r = Math.hypot(x, z);
    if (r < 160) return [19, 60 + (160 - r) / 4];
    if (r > 1000 && isle(x / 160, z / 160) > 0.58) return [19, 58];
    return null;
  }
  const e = elev(x / 520, z / 520) - (Math.hypot(x - 700, z - 500) < 600 ? 0.18 : 0);
  const height = Math.round(40 + e * 70);
  if (e < 0.34) return [1, height];
  if (e < 0.41) return [0, height];
  if (Math.abs(elev(x / 140 + 9, z / 140) - 0.5) < 0.012) return [2, 62];
  if (e < 0.43) return [3, height];
  const t = temp(x / 900, z / 900);
  const m = wet(x / 600, z / 600);
  let b = 4;
  if (t > 0.62) b = m > 0.5 ? 11 : 10;
  else if (t < 0.36) b = m > 0.5 ? 8 : 9;
  else if (m > 0.62) b = 5;
  else if (m > 0.55) b = 6;
  else if (m < 0.36) b = 13;
  else if (e > 0.66) b = 12;
  else if (m > 0.5) b = 7;
  return [b, height];
}

/** Explored ground: around spawn, the castle, the village and the trails between them in
 *  Castle Hill; a patch at spawn elsewhere; BlockyFox's trip in the Nether; nothing in the End. */
function explored(slot: string, dim: MinecraftDim, x: number, z: number): boolean {
  const cx = Math.floor(x / 16);
  const cz = Math.floor(z / 16);
  const near = (px: number, pz: number, r: number) =>
    Math.hypot(cx * 16 + 8 - px, cz * 16 + 8 - pz) < r;
  if (dim === "the_end") return false;
  if (dim === "nether") return slot === "slot2" && (near(20, -20, 140) || near(-180, 140, 90));
  if (slot === "slot2") {
    return near(32, -8, 260) || near(232, -304, 220) || near(-140, 90, 200) || near(424, 140, 120);
  }
  return near(0, 0, 96);
}

function survey(c: [number, number, number]): [number, number, number] {
  const g = c[0] * 0.3 + c[1] * 0.59 + c[2] * 0.11;
  return [0, 1, 2].map((i) => (g + ((c[i] ?? 0) - g) * 0.5) * 0.65) as [number, number, number];
}

const TILE = 256;

/** One 256×256 tile, painted like `mapping.tile`: point samples every 2^zoom blocks. */
export function mockTile(
  slot: string,
  dim: MinecraftDim,
  zoom: number,
  tx: number,
  tz: number,
): HTMLCanvasElement {
  const step = 1 << zoom;
  const x0 = tx * TILE * step;
  const z0 = tz * TILE * step;
  const cv = document.createElement("canvas");
  cv.width = TILE;
  cv.height = TILE;
  const ctx = cv.getContext("2d");
  if (!ctx) return cv;
  const img = ctx.createImageData(TILE, TILE);
  const px = img.data;
  const gain = (dim === "nether" ? 0.01 : 0.06) / step;
  for (let j = 0; j < TILE; j++) {
    for (let i = 0; i < TILE; i++) {
      const x = x0 + i * step;
      const z = z0 + j * step;
      const s = sample(dim, x, z);
      if (!s) continue;
      const [b, h] = s;
      const w = sample(dim, x - step, z)?.[1] ?? h;
      const n = sample(dim, x, z - step)?.[1] ?? h;
      const shade = WATER.has(b)
        ? 0.85 + 0.15 * Math.min(1, Math.max(0, (h - 40) / 22))
        : 1 + Math.max(-0.35, Math.min(0.35, (h - w + (h - n)) * gain));
      const base = BIOMES[b]?.[1] ?? [128, 128, 128];
      let c: [number, number, number] = [base[0] * shade, base[1] * shade, base[2] * shade];
      if (!explored(slot, dim, x, z)) c = survey(c);
      const o = (j * TILE + i) * 4;
      px[o] = c[0];
      px[o + 1] = c[1];
      px[o + 2] = c[2];
      px[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return cv;
}

export function mcMapInfo(slot: string, dim: MinecraftDim): MinecraftMapInfo {
  const castle = slot === "slot2";
  const extent =
    dim === "the_end"
      ? null
      : dim === "nether"
        ? castle
          ? { min_x: -272, max_x: 159, min_z: -160, max_z: 239, chunks: 210 }
          : null
        : castle
          ? { min_x: -352, max_x: 543, min_z: -528, max_z: 271, chunks: 1830 }
          : { min_x: -96, max_x: 95, min_z: -96, max_z: 95, chunks: 31 };
  return {
    slot,
    dimension: dim,
    extent,
    tile_blocks: 256,
    max_zoom: 4,
    layers: ["biome"],
    predicted: true,
  };
}

export function mcMapPoint(
  slot: string,
  dim: MinecraftDim,
  x: number,
  z: number,
): MinecraftMapPoint {
  const s = sample(dim, x, z);
  if (!s) return { x, z, dimension: dim, source: "unknown" };
  const [b, h] = s;
  const name = BIOMES[b]?.[0] ?? "Plains";
  return {
    x,
    z,
    dimension: dim,
    source: explored(slot, dim, x, z) ? "explored" : "survey",
    biome: name,
    biome_id: `minecraft:${name.toLowerCase().replace(/ /g, "_")}`,
    y: WATER.has(b) ? 62 : Math.round(h),
  };
}

export function mcMapPlayers(slot: string, live: boolean): MinecraftMapPlayers {
  const t = nowS();
  const castle = slot === "slot2";
  const players = castle
    ? [
        {
          xuid: "2535411",
          gamertag: "BlockyFox",
          dim: "overworld",
          x: 118,
          y: 71,
          z: -212,
          at: t - 8,
          online: live,
        },
        {
          xuid: "2535412",
          gamertag: "Mira_P",
          dim: "overworld",
          x: 286,
          y: 68,
          z: -352,
          at: t - 3,
          online: live,
        },
        {
          xuid: "2535413",
          gamertag: "Steve42",
          dim: "overworld",
          x: 410,
          y: 63,
          z: 132,
          at: t - 66_000,
          online: false,
        },
        {
          xuid: "2535414",
          gamertag: "Pebble_J",
          dim: "nether",
          x: 60,
          y: 70,
          z: -40,
          at: t - 170_000,
          online: false,
        },
      ]
    : [
        {
          xuid: "2535411",
          gamertag: "BlockyFox",
          dim: "overworld",
          x: 12,
          y: 70,
          z: -6,
          at: t - 500_000,
          online: false,
        },
      ];
  return {
    slot,
    world: slot,
    live: live && castle,
    players: players as MinecraftMapPlayers["players"],
  };
}

const STRUCTS: Record<MinecraftDim, [string, number, number][]> = {
  overworld: [
    ["village", 232, -304],
    ["village", -1000, 747],
    ["village", 1460, -620],
    ["mansion", -3785, 2605],
    ["monument", 1087, 1056],
    ["pillager_outpost", -174, -419],
    ["ancient_city", -1820, -1340],
    ["trail_ruins", 150, 830],
    ["trial_chambers", -720, 920],
    ["stronghold", 1480, -1640],
    ["temple", -1551, 1149],
    ["temple", 1400, 307],
    ["ruins", 700, 380],
    ["shipwreck", 434, 146],
    ["buried_treasure", 308, 258],
    ["ruined_portal", 180, 380],
    ["ruined_portal", -260, -520],
    ["mineshaft", -300, 500],
    ["mineshaft", 472, -138],
  ],
  nether: [
    ["fortress", 60, -40],
    ["bastion_remnant", -180, 140],
    ["ruined_portal", 120, 200],
    ["fortress", -420, -380],
  ],
  the_end: [
    ["end_city", 1100, -400],
    ["end_city", -1240, 860],
  ],
};

export function mcMapStructures(slot: string, dim: MinecraftDim): MinecraftMapStructures {
  const swept = slot === "slot2";
  return {
    slot,
    dimension: dim,
    structures: swept ? STRUCTS[dim].map(([structure, x, z]) => ({ structure, x, z })) : [],
    swept_at: swept ? nowS() - 86_400 : null,
    stale: swept ? null : "never searched",
    sweep: { state: "idle" },
  };
}
