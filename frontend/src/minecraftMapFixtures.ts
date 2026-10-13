// dev:mock's Minecraft map: the reviewed mock's world (docs/mocks/minecraft-map/README.md
// "Shared fixture") drawn procedurally, so the map screen pans over something real-looking
// with no box. Tiles are painted here the way the renderer paints them — the biome table,
// hillshade, and the satellite survey half-desaturated at 65% outside explored ground —
// and the routes answer from the same world. Only ever imported in mock mode.

import type {
  MinecraftDim,
  MinecraftMapEntities,
  MinecraftMapInfo,
  MinecraftMapMarkers,
  MinecraftMapPlaces,
  MinecraftMapPlayers,
  MinecraftMapPoint,
  MinecraftMapStructures,
  MinecraftSession,
  MinecraftTimeline,
  MinecraftTimelineEvent,
  MinecraftTimelinePart,
  MinecraftTrade,
  MinecraftTrailSegment,
  MinecraftVillager,
  MinecraftVillagerCard,
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
    spawn: dim === "overworld" ? (castle ? { x: 32, z: -8 } : { x: 0, z: 0 }) : null,
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

/** `scan` (the mock's `?mcscan=`) puts BlockyFox's Tricorder on a live scan for diamond
 *  (§M25): `found` one 13 blocks north-east and 6 below, `none` nearby, or kept while
 *  they're `offline`. */
export function mcMapPlayers(
  slot: string,
  live: boolean,
  scan: "" | "found" | "none" | "offline" = "",
): MinecraftMapPlayers {
  const t = nowS();
  const castle = slot === "slot2";
  const log = fixtureLog();
  // Castle Hill's players are where the travel log last saw them, as on the box.
  const players = castle
    ? FX_PLAYERS.flatMap((p) => {
        const segs = log.trails.get(p.xuid) ?? [];
        const seg = segs[segs.length - 1];
        const last = seg?.points[seg.points.length - 1];
        if (!seg || !last) return [];
        const on = log.sessions.some((s) => s.xuid === p.xuid && s.end === null);
        return [
          {
            xuid: p.xuid,
            gamertag: p.gamertag,
            dim: seg.dim,
            x: last[1],
            y: last[2],
            z: last[3],
            at: on ? t - 8 : last[0],
            online: live && on && !(scan === "offline" && p.gamertag === "BlockyFox"),
            // Dave has BlockyFox's Tricorder on some diamonds to the north-east.
            target:
              p.gamertag === "BlockyFox"
                ? {
                    dim: "overworld" as MinecraftDim,
                    x: 362,
                    y: -58,
                    z: -437,
                    label: "Diamonds",
                    set_at: t - 600,
                  }
                : null,
            scan:
              p.gamertag === "BlockyFox" && scan
                ? {
                    label: "diamond",
                    state: scan === "none" ? ("none" as const) : ("found" as const),
                    found:
                      scan === "none"
                        ? null
                        : {
                            dim: seg.dim,
                            x: Math.floor(last[1]) + 9,
                            y: Math.floor(last[2]) - 6,
                            z: Math.floor(last[3]) - 9,
                          },
                    at: t - 40,
                    set_at: t - 900,
                  }
                : null,
          },
        ];
      })
    : [
        {
          xuid: "2535411",
          gamertag: "BlockyFox",
          dim: "overworld" as MinecraftDim,
          x: 12,
          y: 70,
          z: -6,
          at: t - 500_000,
          online: false,
        },
      ];
  return { slot, world: slot, live: live && castle, players };
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

// The markers mock's world (docs/mocks/minecraft-map-markers/, "the world's data"): three
// beacons, two of them overlapping and one up in a sky base, and three gates — open to the
// Tree farm, dormant, and open into the Nether.
export function mcMapMarkers(slot: string): MinecraftMapMarkers {
  const t = nowS();
  if (slot !== "slot2") return { slot, world: slot, beacons: [], gates: [], as_of: null };
  const ow = "overworld" as MinecraftDim;
  return {
    slot,
    world: "Castle Hill",
    beacons: [
      { dim: ow, x: 60, y: 72, z: -40, placed_by: "Mira_P", placed_at: t - 86_400 },
      { dim: ow, x: 90, y: 74, z: -10, placed_by: "BlockyFox", placed_at: t - 20_000 },
      { dim: ow, x: -150, y: 210, z: 96, placed_by: "BlockyFox", placed_at: t - 4_000 },
    ],
    gates: [
      {
        id: 1,
        dim: ow,
        x: 28,
        y: 71,
        z: 14,
        axis: "x",
        planter: "BlockyFox",
        state: "open",
        target: { place: "Tree farm", dim: ow, x: -160, y: 68, z: -140 },
        planted_at: t - 100_000,
        aimed_at: t - 30_000,
      },
      {
        id: 2,
        dim: ow,
        x: -70,
        y: 66,
        z: 60,
        axis: "z",
        planter: "Mira_P",
        state: "dormant",
        target: null,
        planted_at: t - 25_000,
        aimed_at: null,
      },
      {
        id: 3,
        dim: ow,
        x: 2,
        y: 70,
        z: -78,
        axis: "x",
        planter: "BlockyFox",
        state: "open",
        target: { place: "Nether base", dim: "nether", x: -20, y: 72, z: 40 },
        planted_at: t - 20_000,
        aimed_at: t - 19_000,
      },
    ],
    as_of: t - 60,
  };
}

// ---- saved places, villagers and pets (GUI gate docs/mocks/minecraft-map-layers/) ----

// The layers mock's world: BlockyFox's Home (under his beacon), Tree farm (gate 1 leads
// there, by its block) and Nether base; Mira_P's Bakery (under her beacon) and Melon farm;
// Steve42's Mine outpost underground and Lookout in the sky.
export function mcMapPlaces(slot: string): MinecraftMapPlaces {
  const t = nowS();
  if (slot !== "slot2") return { slot, world: slot, places: [], as_of: null };
  const ow = "overworld" as MinecraftDim;
  const row = (
    id: string,
    who: [string, string],
    name: string,
    at: [number, number | null, number],
    dim: MinecraftDim,
    extra: Partial<MinecraftMapPlaces["places"][number]> = {},
  ) => ({
    id,
    xuid: who[0],
    gamertag: who[1],
    name,
    dim,
    x: at[0],
    y: at[1],
    z: at[2],
    saved_at: t - 172_800,
    moved: false,
    covered: [],
    gates: [],
    gates_moved: [],
    ...extra,
  });
  const fox: [string, string] = ["2535411", "BlockyFox"];
  const mira: [string, string] = ["2535412", "Mira_P"];
  const steve: [string, string] = ["2535413", "Steve42"];
  return {
    slot,
    world: "slot2",
    places: [
      row("p1", fox, "Home", [70, 71, 12], ow, {
        covered: [{ x: 90, y: 74, z: -10, placed_by: "BlockyFox" }],
      }),
      row("p2", fox, "Tree farm", [-160, 68, -140], ow, { gates: [1] }),
      row("p3", fox, "Nether base", [-20, 72, 40], "nether", { gates: [3], moved: true }),
      row("p4", mira, "Bakery", [40, 70, -56], ow, {
        covered: [{ x: 60, y: 72, z: -40, placed_by: "Mira_P" }],
        saved_at: t - 86_400,
      }),
      row("p5", mira, "Melon farm", [-20, 66, 96], ow, { saved_at: t - 86_400 }),
      row("p6", steve, "Mine outpost", [-110, -12, 40], ow, { saved_at: t - 259_200 }),
      row("p7", steve, "Lookout", [-150, 140, -236], ow, { saved_at: t - 86_400 }),
    ],
    as_of: t - 86_400,
  };
}

function rnd(i: number) {
  const v = Math.sin(i * 12.9898 + 4.1414) * 43758.5453;
  return v - Math.floor(v);
}

// A village of 23 round the located one at X 232 · Z −304, and a group of 8 with no
// located village; the librarian the owner taps is an Expert who sells Mending.
const V1 = [
  ...["librarian", "librarian", "librarian", "librarian"],
  ...["farmer", "farmer", "farmer", "farmer", "farmer", "cleric", "cleric"],
  ...["armorer", "armorer", "toolsmith", "weaponsmith", "cartographer"],
  ...["fisherman", "fisherman", "shepherd", "fletcher", "mason", "unemployed", "nitwit"],
];
const V2 = ["librarian", "farmer", "farmer", "cleric", "mason", "unemployed", "unemployed"];

function fxVillagers(): MinecraftVillager[] {
  const one = (job: string, i: number, cx: number, cz: number, village: string, spread: number) => {
    const v: MinecraftVillager = {
      id: String(-8589934000 - i),
      job,
      level: Math.floor(rnd(i) * 4),
      locked_in: rnd(i + 50) > 0.35,
      name: "",
      x: cx + Math.round((rnd(i + 7) - 0.5) * spread),
      y: 66,
      z: cz + Math.round((rnd(i + 19) - 0.5) * spread),
      village,
      trades: 4,
      enchants: [],
    };
    return v;
  };
  const a = V1.map((job, i) => one(job, i, 232, -304, "v1", 56));
  const b = [...V2, "fisherman"].map((job, i) => one(job, i + 90, 100, 140, "v2", 22));
  const lib = a[0] as MinecraftVillager;
  Object.assign(lib, { level: 3, locked_in: true, x: 238, z: -298, enchants: ["mending"] });
  Object.assign(a[2] as MinecraftVillager, { level: 4, locked_in: true, enchants: ["mending"] });
  return [...a, ...b];
}

export function mcMapEntities(slot: string, dim: MinecraftDim): MinecraftMapEntities {
  const t = nowS();
  const empty: MinecraftMapEntities = {
    dimension: dim,
    villages: [],
    villagers: [],
    villagers_truncated: false,
    pets: [],
    named: [],
    traders: 0,
    live: false,
    as_of: t - 360,
    age_s: 360,
  };
  if (slot !== "slot2" || dim !== "overworld") return empty;
  const villagers = fxVillagers();
  const jobs = (id: string) => {
    const out: Record<string, number> = {};
    for (const v of villagers) if (v.village === id) out[v.job] = (out[v.job] ?? 0) + 1;
    return out;
  };
  const pet = (
    id: string,
    name: string,
    kind: string,
    owner: [string, string] | null,
    at: [number, number],
    sitting: boolean,
    coat: string[],
  ) => ({
    id,
    kind,
    name,
    tamed: true,
    owner: owner ? owner[0] : "-77",
    owner_name: owner ? owner[1] : "",
    sitting,
    baby: false,
    coat,
    x: at[0],
    y: 70,
    z: at[1],
  });
  return {
    ...empty,
    villages: [
      { id: "v1", x: 230, z: -302, count: 23, jobs: jobs("v1") },
      { id: "v2", x: 100, z: 140, count: 8, jobs: jobs("v2") },
    ],
    villagers,
    pets: [
      pet("-101", "Rex", "wolf", ["-4294967295", "BlockyFox"], [52, 52], true, ["pale"]),
      pet("-102", "Whiskers", "cat", ["-4294967296", "Mira_P"], [30, -60], false, ["calico"]),
      pet("-103", "Kiwi", "parrot", ["-4294967296", "Mira_P"], [-10, -92], true, ["red"]),
      pet("-104", "Biscuit", "horse", ["-4294967297", "Steve42"], [-96, -6], false, []),
      pet("-105", "Luna", "wolf", ["-4294967297", "Steve42"], [-166, 6], true, ["black"]),
      pet("-106", "", "cat", null, [150, -40], true, ["tabby"]),
    ],
    named: [
      { ...pet("-201", "Clover", "cow", null, [-30, 70], false, []), tamed: false, owner: null },
      { ...pet("-202", "Hopper", "rabbit", null, [10, 132], false, []), tamed: false, owner: null },
    ],
    traders: 1,
    live: true,
  };
}

const TRADES: MinecraftTrade[] = [
  [0, ["minecraft:paper", 24], null, ["minecraft:emerald", 1], [], 3, 16],
  [0, ["minecraft:emerald", 9], null, ["minecraft:bookshelf", 1], [], 0, 12],
  [
    1,
    ["minecraft:emerald", 12],
    ["minecraft:book", 1],
    ["minecraft:enchanted_book", 1],
    [["mending", 1]],
    2,
    12,
  ],
  [1, ["minecraft:ink_sac", 5], null, ["minecraft:emerald", 1], [], 12, 12],
  [2, ["minecraft:emerald", 1], null, ["minecraft:glass", 4], [], 0, 12],
  [3, ["minecraft:emerald", 5], null, ["minecraft:clock", 1], [], 1, 12],
  [4, ["minecraft:emerald", 20], null, ["minecraft:name_tag", 1], [], 0, 12],
].map(
  ([tier, buy, buy2, sell, enchantments, uses, max]) =>
    ({
      tier,
      buy,
      buy2,
      sell,
      enchantments,
      uses,
      max_uses: max,
      used_up: uses === max,
    }) as MinecraftTrade,
);

export function mcMapVillager(slot: string, id: string): MinecraftVillagerCard {
  const t = nowS();
  const v = slot === "slot2" ? fxVillagers().find((q) => q.id === id) : undefined;
  if (!v) return { found: null, as_of: t - 360, age_s: 360 };
  const mine = v.enchants.includes("mending")
    ? TRADES
    : TRADES.filter((q) => !q.enchantments.length);
  return {
    found: {
      id: v.id,
      job: v.job,
      level: v.level,
      level_name: ["Novice", "Apprentice", "Journeyman", "Expert", "Master"][v.level] ?? "Novice",
      locked_in: v.locked_in,
      name: v.name,
      x: v.x,
      y: v.y,
      z: v.z,
      dimension: "overworld",
      trades: mine.filter((q) => q.tier <= v.level),
      locked_trades: mine.filter((q) => q.tier > v.level).length,
    },
    as_of: t - 360,
    age_s: 360,
  };
}

/** The goal count a place card asks for: the map's players keyed as the goals fixture
 *  keys them. */
export const GOAL_KEYS: Record<string, string> = {
  "2535411": "x-BlockyFox",
  "2535412": "x-Mira_P",
  "2535413": "x-Steve42",
};

// ---- the travel log behind the timeline (M8a): the reviewed fixture's week ----
//
// README "Shared fixture": 17 sessions from six days ago to now (Oct 4–10 in the mock,
// whose "now" is Sat Oct 10, 2:40 pm — pin the clock there and the days and times match),
// the four players, their deaths, and BlockyFox's 3h 42m session the day before yesterday's
// evening: castle → portal → Nether fortress, dies in lava, respawns at his bed, then the
// shipwreck, the village and home. Today's sessions are placed back from the real now, so
// "on now" is true whatever the time.

interface FxPlayer {
  xuid: string;
  gamertag: string;
  bed: [number, number] | null;
}

const FX_PLAYERS: FxPlayer[] = [
  { xuid: "2535411", gamertag: "BlockyFox", bed: [-136, 94] },
  { xuid: "2535412", gamertag: "Mira_P", bed: [226, -296] },
  { xuid: "2535413", gamertag: "Steve42", bed: null },
  { xuid: "2535414", gamertag: "Pebble_J", bed: null },
];
const SPAWN: [number, number] = [32, -8];
const PORTAL_O: [number, number] = [-40, 40];
const PORTAL_N: [number, number] = [-5, 5];
const VILLAGE: [number, number] = [232, -304];
const CASTLE: [number, number] = [-140, 90];

interface Way {
  p?: [number, number];
  dwell?: number;
  speed?: number;
  portal?: MinecraftDim;
  respawn?: boolean;
  died?: [string, string | null, number];
}

interface FxLog {
  key: number;
  now: number;
  sessions: MinecraftSession[];
  trails: Map<string, MinecraftTrailSegment[]>;
  events: MinecraftTimelineEvent[];
}

function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

const groundY = (dim: MinecraftDim) => (dim === "nether" ? 64 : 70);

function buildLog(): FxLog {
  const now = Math.floor(nowS());
  // Day `d` of the mock's Oct 4–10 week, as a day this many days before today.
  const at = (d: number, h: number, m: number) => {
    const t = new Date();
    return new Date(t.getFullYear(), t.getMonth(), t.getDate() - (10 - d), h, m).getTime() / 1000;
  };
  const r = rng(4242);
  const wob = noise(31);
  const sessions: MinecraftSession[] = [];
  const trails = new Map<string, MinecraftTrailSegment[]>();
  const events: MinecraftTimelineEvent[] = [];

  function session(who: FxPlayer, start: number, end: number | null, plan: Way[]) {
    const id = `fx-${sessions.length + 1}`;
    const stop = end ?? now;
    const segs: MinecraftTrailSegment[] = [];
    const distance: Partial<Record<MinecraftDim, number>> = {};
    let t = start;
    let d: MinecraftDim = "overworld";
    let [x, z] = plan[0]?.p ?? SPAWN;
    const ev = (kind: MinecraftTimelineEvent["kind"], extra: Partial<MinecraftTimelineEvent>) =>
      events.push({
        kind,
        xuid: who.xuid,
        gamertag: who.gamertag,
        at: t,
        dim: d,
        x: Math.round(x),
        y: groundY(d),
        z: Math.round(z),
        ...extra,
      });
    const push = (nx: number, nz: number, fresh = false) => {
      let seg = segs[segs.length - 1];
      const last = seg?.points[seg.points.length - 1];
      if (!seg || fresh || seg.dim !== d) {
        seg = { session: id, dim: d, points: [] };
        segs.push(seg);
      } else if (last) {
        const step = Math.hypot(nx - last[1], nz - last[3]);
        if (step < 4) return; // the sidecar keeps a sample only after movement
        distance[d] = (distance[d] ?? 0) + step;
      }
      seg.points.push([t, Math.round(nx), groundY(d), Math.round(nz)]);
    };
    push(x, z);
    ev("join", {});
    for (const w of plan.slice(1)) {
      if (t >= stop) break;
      if (w.portal) {
        t += 6;
        const from = d;
        d = w.portal;
        [x, z] = d === "nether" ? PORTAL_N : PORTAL_O;
        push(x, z, true);
        ev("dimension", { from_dim: from });
        continue;
      }
      if (w.respawn) {
        t += 9;
        d = "overworld";
        [x, z] = who.bed ?? SPAWN;
        push(x, z, true);
        ev("respawn", {});
        continue;
      }
      const [tx, tz] = w.p ?? [x, z];
      const dist = Math.hypot(tx - x, tz - z);
      const steps = Math.max(1, Math.round(dist / ((w.speed ?? 4.2) * 10)));
      const [sx, sz] = [x, z];
      const ph = r() * 100;
      for (let q = 1; q <= steps && t < stop; q++) {
        t += 10;
        const f = q / steps;
        const off = Math.sin(f * Math.PI) * 26 * (wob(ph + f * 3, 1, 2) - 0.5) * 2;
        x = sx + (tx - sx) * f - ((tz - sz) / (dist || 1)) * off;
        z = sz + (tz - sz) * f + ((tx - sx) / (dist || 1)) * off;
        push(x, z);
      }
      if (w.died) {
        t += 4;
        const [cause, killer, y] = w.died;
        ev("death", { cause, killer, y });
      }
      if (w.dwell) {
        const until = Math.min(stop, t + w.dwell * 60);
        while (t + 60 < until) {
          t += 60;
          push(tx + (r() - 0.5) * 18, tz + (r() - 0.5) * 18);
        }
        t = until;
      }
    }
    t = stop;
    if (end !== null) ev("leave", {});
    const first = segs[0]?.points[0];
    sessions.push({
      id,
      xuid: who.xuid,
      gamertag: who.gamertag,
      start,
      end,
      last_seen: Math.min(now, stop),
      distance: Object.fromEntries(Object.entries(distance).map(([k, v]) => [k, Math.round(v)])),
      dims: DIM_ORDER.filter((k) => segs.some((g) => g.dim === k)),
      deaths: 0,
      first: first && segs[0] ? { dim: segs[0].dim, x: first[1], z: first[3] } : null,
    });
    trails.set(who.xuid, [...(trails.get(who.xuid) ?? []), ...segs]);
  }

  const [B, M, S, P] = FX_PLAYERS as [FxPlayer, FxPlayer, FxPlayer, FxPlayer];
  const zombie = "minecraft:zombie";
  session(B, at(4, 16, 2), at(4, 17, 31), [
    { p: SPAWN },
    { p: [-20, 40] },
    { p: CASTLE, dwell: 34 },
    { p: [40, 130] },
    { p: [10, 20], dwell: 6 },
  ]);
  session(M, at(4, 16, 10), at(4, 17, 20), [
    { p: SPAWN },
    { p: [120, -140] },
    { p: VILLAGE, dwell: 40 },
  ]);
  session(M, at(5, 17, 0), at(5, 18, 10), [{ p: VILLAGE }, { p: [196, -262], dwell: 52 }]);
  session(P, at(5, 19, 10), at(5, 19, 48), [{ p: SPAWN }, { p: [60, 40], dwell: 22 }]);
  session(B, at(6, 15, 31), at(6, 16, 48), [
    { p: CASTLE },
    { p: [-250, -90] },
    { p: [-330, -230], dwell: 8 },
    { p: [-372, -262], died: ["fall", null, 131] },
    { respawn: true },
    { p: [-100, 150], dwell: 22 },
  ]);
  session(S, at(6, 18, 0), at(6, 18, 40), [
    { p: SPAWN },
    { p: [-120, -60] },
    { p: [-40, -150] },
    { p: SPAWN, dwell: 4 },
  ]);
  session(B, at(7, 10, 20), at(7, 11, 5), [
    { p: CASTLE },
    { p: [-200, 40] },
    { p: CASTLE, dwell: 22 },
  ]);
  session(B, at(7, 16, 5), at(7, 17, 12), [
    { p: CASTLE },
    { p: [60, 200] },
    { p: [-60, 300], died: ["entityAttack", zombie, 66] },
    { respawn: true },
    { p: [-150, 120], dwell: 20 },
  ]);
  session(S, at(7, 19, 30), at(7, 20, 5), [
    { p: SPAWN },
    { p: [-40, -150], dwell: 12 },
    { p: SPAWN },
  ]);
  session(M, at(8, 17, 0), at(8, 18, 30), [
    { p: VILLAGE },
    { p: [196, -262], dwell: 34 },
    { p: [150, -200], died: ["entityExplosion", "minecraft:creeper", 69] },
    { respawn: true },
    { p: [196, -262], dwell: 40 },
  ]);
  session(P, at(8, 18, 0), at(8, 18, 40), [
    { p: SPAWN },
    { p: [60, 40] },
    { p: [90, 60], died: ["entityAttack", zombie, 70] },
    { respawn: true },
    { p: [10, 10], dwell: 10 },
  ]);
  session(B, at(9, 16, 10), at(9, 19, 52), [
    { p: CASTLE, dwell: 12 },
    { p: PORTAL_O },
    { portal: "nether" },
    { p: [30, -20] },
    { p: [60, -40], dwell: 24 },
    { p: [86, -30] },
    { p: [41, -58], died: ["lava", null, 33] },
    { respawn: true },
    { p: [100, 60] },
    { p: [300, 100] },
    { p: [430, 150], dwell: 16, speed: 3 },
    { p: [380, 40] },
    { p: [250, -150] },
    { p: VILLAGE, dwell: 25 },
    { p: [120, -40] },
    { p: CASTLE, dwell: 20 },
  ]);
  session(M, at(9, 17, 0), at(9, 18, 15), [
    { p: VILLAGE },
    { p: [300, -420] },
    { p: [120, -470] },
    { p: VILLAGE, dwell: 6 },
  ]);
  session(S, at(9, 19, 20), at(9, 20, 14), [
    { p: SPAWN },
    { p: [200, 40] },
    { p: [380, 120] },
    { p: [434, 146], died: ["drowning", null, 48], speed: 2.5 },
    { respawn: true },
    { p: [60, -30], dwell: 30 },
  ]);
  // Today: placed back from now, so the two still on are on whatever the time.
  session(M, now - 5 * 3600, now - 5 * 3600 + 45 * 60, [
    { p: VILLAGE },
    { p: [196, -262], dwell: 30 },
  ]);
  session(B, now - 42 * 60, null, [
    { p: CASTLE },
    { p: [-20, -60] },
    { p: [130, -180] },
    { p: [196, -262], dwell: 8 },
    { p: [118, -212], dwell: 60 },
  ]);
  session(M, now - 7 * 60, null, [
    { p: VILLAGE },
    { p: [330, -400] },
    { p: [286, -352], dwell: 60 },
  ]);

  events.sort((a, b) => a.at - b.at);
  for (const s of sessions) {
    s.deaths = events.filter(
      (e) => e.kind === "death" && e.xuid === s.xuid && e.at >= s.start && e.at <= (s.end ?? now),
    ).length;
  }
  for (const [k, segs] of trails) {
    trails.set(
      k,
      segs.sort((a, b) => (a.points[0]?.[0] ?? 0) - (b.points[0]?.[0] ?? 0)),
    );
  }
  return { key: Math.floor(now / 60), now, sessions, trails, events };
}

const DIM_ORDER: MinecraftDim[] = ["overworld", "nether", "the_end"];
let LOG: FxLog | null = null;
let FOG: { key: number; by: Map<string, Map<string, number>> } | null = null;

/** The fixture's week, rebuilt once a minute so "now" and "on now" stay true. */
export function fixtureLog(): FxLog {
  if (!LOG || LOG.key !== Math.floor(nowS() / 60)) LOG = buildLog();
  return LOG;
}

/** Each player's fog of war per dimension: chunk → first seen, the travel log's 4-chunk
 *  reveal. */
function fog(log: FxLog) {
  if (FOG?.key === log.key) return FOG.by;
  const by = new Map<string, Map<string, number>>();
  for (const [xuid, segs] of log.trails) {
    for (const seg of segs) {
      const key = `${xuid}|${seg.dim}`;
      const m = by.get(key) ?? new Map<string, number>();
      by.set(key, m);
      let lx = Number.NaN;
      let lz = Number.NaN;
      for (const [t, x, , z] of seg.points) {
        const cx = Math.floor(x / 16);
        const cz = Math.floor(z / 16);
        if (cx === lx && cz === lz) continue;
        lx = cx;
        lz = cz;
        for (let dz = -4; dz <= 4; dz++) {
          for (let dx = -4; dx <= 4; dx++) {
            const k = `${cx + dx},${cz + dz}`;
            if (t < (m.get(k) ?? Number.POSITIVE_INFINITY)) m.set(k, t);
          }
        }
      }
    }
  }
  FOG = { key: log.key, by };
  return by;
}

export function mcTimeline(
  slot: string,
  from: number,
  to: number,
  opts: { include: MinecraftTimelinePart[]; players: string[]; dim: MinecraftDim },
): MinecraftTimeline {
  const log = fixtureLog();
  const castle = slot === "slot2";
  const wanted = (x: string) => !opts.players.length || opts.players.includes(x);
  const out: MinecraftTimeline = { slot, world: slot, from, to, now: nowS() };
  if (opts.include.includes("trails")) {
    out.truncated = false;
    out.trails = castle
      ? FX_PLAYERS.filter((p) => wanted(p.xuid)).flatMap((p) => {
          const segments = (log.trails.get(p.xuid) ?? [])
            .map((g) => ({ ...g, points: g.points.filter(([t]) => t >= from && t <= to) }))
            .filter((g) => g.points.length);
          const samples = segments.reduce((a, g) => a + g.points.length, 0);
          return samples
            ? [{ xuid: p.xuid, gamertag: p.gamertag, samples, downsampled: false, segments }]
            : [];
        })
      : [];
  }
  if (opts.include.includes("events")) {
    out.events = castle
      ? log.events.filter((e) => e.at >= from && e.at <= to && wanted(e.xuid))
      : [];
  }
  if (opts.include.includes("sessions")) {
    out.sessions = castle ? [...log.sessions].sort((a, b) => b.start - a.start) : [];
  }
  if (opts.include.includes("explored")) {
    const best = new Map<string, number>();
    if (castle) {
      for (const [key, chunks] of fog(log)) {
        const [xuid, dim] = key.split("|");
        if (dim !== opts.dim || !wanted(xuid ?? "")) continue;
        for (const [k, t] of chunks) {
          if (t <= to && t < (best.get(k) ?? Number.POSITIVE_INFINITY)) best.set(k, t);
        }
      }
    }
    out.explored = {
      dim: opts.dim,
      chunks: [...best].map(([k, t]) => {
        const [cx, cz] = k.split(",").map(Number);
        return [cx ?? 0, cz ?? 0, t] as [number, number, number];
      }),
      truncated: false,
    };
  }
  return out;
}
