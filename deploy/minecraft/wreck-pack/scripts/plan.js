// The spawn wreck's planning, kept free of @minecraft/* so node can test it
// (docs/plans/MINECRAFT_BEDROCK_PLAN.md §SW2). Everything here is a pure function of
// what the survey read and the build's metadata in data.js; main.js does the reading and
// the placing.

// Things that stand on the ground rather than being it: looked through when finding
// the ground, and cleared from the site.
const PLANTS = new Set([
  "short_grass", "tall_grass", "fern", "large_fern", "dead_bush", "vine", "snow_layer",
  "sweet_berry_bush", "bamboo", "sugar_cane", "cactus", "moss_carpet", "azalea",
  "flowering_azalea", "mangrove_roots", "mangrove_propagule", "glow_lichen",
  "big_dripleaf", "small_dripleaf", "pink_petals", "leaf_litter", "bush", "firefly_bush",
  "short_dry_grass", "tall_dry_grass", "pumpkin", "melon_block", "brown_mushroom",
  "red_mushroom", "brown_mushroom_block", "red_mushroom_block", "mushroom_stem",
  "cocoa", "bee_nest", "pale_moss_carpet", "pale_hanging_moss", "creaking_heart",
  "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "red_tulip",
  "orange_tulip", "white_tulip", "pink_tulip", "oxeye_daisy", "cornflower",
  "lily_of_the_valley", "sunflower", "lilac", "rose_bush", "peony", "torchflower",
  "open_eyeblossom", "closed_eyeblossom", "wildflowers", "cactus_flower", "air",
]);
const WATER = new Set([
  "water", "flowing_water", "seagrass", "tall_seagrass", "kelp", "bubble_column",
  "ice", "packed_ice", "blue_ice", "frosted_ice", "lava", "flowing_lava", "waterlily",
]);

export const bare = (id) => (id || "").replace(/^minecraft:/, "");

/** Not ground: plants, trees, snow cover — the survey reads down through these. */
export function standsOnGround(id) {
  const n = bare(id);
  return (
    PLANTS.has(n) ||
    /_(leaves|log|wood|sapling|stem|hyphae|propagule)$/.test(n) ||
    /^stripped_/.test(n)
  );
}

export const isWater = (id) => WATER.has(bare(id));

// The crater and skid mark in scorched versions of the local ground. `dig` is the
// crater floor and trail (weighted by repetition), `fill` what goes under the hull
// where the ground drops away — never sand or gravel, which would fall.
const GROUNDS = [
  [/^(sand|suspicious_sand)$/, { dig: ["sandstone", "sandstone", "sand", "gravel"], fill: "sandstone", rim: "sandstone" }],
  [/^red_sand$/, { dig: ["red_sandstone", "red_sandstone", "red_sand", "gravel"], fill: "red_sandstone", rim: "red_sandstone" }],
  [/terracotta$/, { dig: ["red_sandstone", "coarse_dirt", "terracotta"], fill: "terracotta", rim: "red_sandstone" }],
  [/^(mud|muddy_mangrove_roots|packed_mud)$/, { dig: ["packed_mud", "mud", "coarse_dirt"], fill: "packed_mud", rim: "mud" }],
  [/^(snow|powder_snow|snow_block)$/, { dig: ["coarse_dirt", "gravel", "dirt"], fill: "dirt", rim: "snow" }],
  [/^(stone|andesite|diorite|granite|deepslate|tuff|calcite|cobblestone|gravel|dripstone_block|mossy_cobblestone)$/,
    { dig: ["cobblestone", "gravel", "andesite", "cobblestone"], fill: "stone", rim: "cobblestone" }],
];
const DEFAULT_GROUND = { dig: ["coarse_dirt", "coarse_dirt", "dirt", "gravel"], fill: "dirt", rim: "dirt" };

export function materialsFor(surfaceId) {
  const n = bare(surfaceId);
  for (const [re, m] of GROUNDS) if (re.test(n)) return m;
  return DEFAULT_GROUND;
}

/** A stable 0..1 value per column, so the same world always gets the same site. */
export function hash01(x, z, salt = 0) {
  let h = (Math.imul(x | 0, 73856093) ^ Math.imul(z | 0, 19349663) ^ Math.imul(salt, 83492791)) >>> 0;
  h = Math.imul(h ^ (h >>> 15), 2246822519) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 3266489917) >>> 0;
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

export const pick = (list, x, z, salt) => list[Math.floor(hash01(x, z, salt) * list.length)];

/**
 * Where the pod comes down: the structure's minimum corner (x, z) and the surface y its
 * deck sits on. `ground(x, z)` → {y, top, water} or null (unread). Candidates are every
 * other column within `radius` of spawn. Only a wet footprint rules one out: steep
 * ground costs score (the build terraces it), and so does water where the parachute
 * lands (it floats). Flattest wins, then driest, then nearest; with no dry footprint
 * anywhere, the pod splashes down at spawn.
 */
export function chooseSite(ground, spawn, pod, radius = 24) {
  const [sx, , sz] = pod.size;
  let best = null;
  for (let dx = -radius; dx <= radius; dx += 2) {
    for (let dz = -radius; dz <= radius; dz += 2) {
      if (dx * dx + dz * dz > radius * radius) continue;
      const ox = spawn.x + dx - Math.floor(sx / 2);
      const oz = spawn.z + dz - Math.floor(sz / 2);
      const ys = [];
      let ok = true;
      for (let x = ox; x < ox + sx && ok; x += 1) {
        for (let z = oz; z < oz + sz && ok; z += 1) {
          const g = ground(x, z);
          if (!g || g.water) ok = false;
          else ys.push(g.y);
        }
      }
      if (!ok) continue;
      ys.sort((a, b) => a - b);
      const spread = ys[ys.length - 1] - ys[0];
      let wet = 0;
      const nx = ox + Math.round(pod.nose[0]);
      const nz = oz + Math.round(pod.nose[2]);
      for (let a = 4; a <= 20; a += 4) {
        for (let c = -4; c <= 4; c += 4) {
          const g = ground(nx + a, nz + c);
          if (!g || g.water) wet += 1;
        }
      }
      const median = ys[Math.floor(ys.length / 2)];
      const score = spread * 10 + wet * 3 + Math.hypot(dx, dz) * 0.2;
      if (!best || score < best.score) best = { x: ox, z: oz, y: median, score, splash: false };
    }
  }
  if (best) return best;
  const g = ground(spawn.x, spawn.z);
  return {
    x: spawn.x - Math.floor(sx / 2),
    z: spawn.z - Math.floor(sz / 2),
    y: g ? g.y : 62,
    score: Infinity,
    splash: true,
  };
}

/** The pod placed at `site`: its corner, and its anchor points in world coordinates. */
export function podFrame(site, pod) {
  const deckRel = pod.hatch_out[1];
  // The deck sits one above the surface, as it did on the design's flat ground. For a
  // splashdown the "surface" is the sea's: the belly rides in the water, the deck and
  // hatch just above it, so the sea never comes in.
  const oy = site.y + 1 - deckRel;
  const at = (p) => [site.x + p[0], oy + p[1], site.z + p[2]];
  return {
    origin: { x: site.x, y: oy, z: site.z },
    hatch: at(pod.hatch_out),
    shield: at(pod.shield),
    nose: at(pod.nose),
    barrels: pod.barrels.map(at),
    deckY: oy + deckRel,
    axis: pod.axis_xz,
    side: pod.hatch_dir,
    splash: Boolean(site.splash),
  };
}

/**
 * The crater round the heat shield, the soil shoved up ahead of it, and the skid mark
 * it came in on: per column, how far to lower (dig > 0) or raise (raise > 0) the ground.
 */
export function craterColumns(frame) {
  const [sx, , sz] = frame.shield;
  const [ax, az] = frame.axis;
  const back = [-ax, -az]; // the pod came in shield-first along its own axis
  const across = [-back[1], back[0]];
  const out = [];
  for (let x = Math.floor(sx) - 10; x <= Math.floor(sx) + 30; x += 1) {
    for (let z = Math.floor(sz) - 30; z <= Math.floor(sz) + 30; z += 1) {
      const dx = x + 0.5 - sx;
      const dz = z + 0.5 - sz;
      const r = Math.hypot(dx, dz);
      const along = dx * back[0] + dz * back[1];
      const side = dx * across[0] + dz * across[1];
      let dig = 0;
      let raise = 0;
      let kind = null;
      if (r < 6) {
        dig = 2 * (1 - (r / 6) ** 2);
        kind = "crater";
      } else if (r < 8.5 && along < 1) {
        raise = 1.6 * (1 - Math.abs(r - 7.2) / 1.3);
        kind = "rim";
      }
      if (along > 3 && along < 26 && Math.abs(side) < 3.2 - along * 0.06) {
        dig = Math.max(dig, 1.2 * (1 - along / 26));
        kind = kind === "crater" ? "crater" : "trail";
      }
      const d = Math.round(dig);
      const u = Math.round(raise);
      if (kind && (d > 0 || u > 0 || kind === "trail")) out.push({ x, z, dig: d, raise: u, kind });
    }
  }
  return out;
}

/**
 * The parachute dragged out flat behind the collar: carpet gores in orange and white,
 * a couple of wool folds standing up, and three lines back to the pod. Per column:
 * {x, z, block, fold}. Ported from the signed-off design (spikes/…/gen_pod.py).
 */
export function canopyCells(frame) {
  const [nx, , nz] = frame.nose;
  const [ax, az] = frame.axis;
  const [cx, cz] = [-az, ax]; // across the canopy; any perpendicular will do
  const CA = 6.5;
  const CC = 5.5;
  const CD = 14;
  const cells = new Map();
  for (let x = Math.floor(nx) - 4; x <= Math.floor(nx) + 26; x += 1) {
    for (let z = Math.floor(nz) - 26; z <= Math.floor(nz) + 26; z += 1) {
      const dx = x + 0.5 - nx;
      const dz = z + 0.5 - nz;
      const a = dx * ax + dz * az - CD;
      const c = dx * cx + dz * cz;
      const e = (a / CA) ** 2 + (c / CC) ** 2;
      if (e > 1 + 0.12 * (hash01(x, z, 3) * 2 - 1)) continue;
      const gore = Math.floor(((Math.atan2(c, a) * 180) / Math.PI + 180) / 30);
      const orange = gore % 2 === 1;
      const fold = e > 0.45 && e < 0.62 && hash01(x, z, 9) > 0.4;
      cells.set(`${x},${z}`, {
        x, z,
        block: fold ? (orange ? "orange_wool" : "white_wool") : orange ? "orange_carpet" : "white_carpet",
        fold,
      });
    }
  }
  for (const c of [-2.5, 0, 2.5]) {
    for (let t = 1; t <= CD - CA + 0.5; t += 0.25) {
      const k = t / (CD - CA);
      const x = Math.floor(nx + ax * t + cx * c * k);
      const z = Math.floor(nz + az * t + cz * c * k);
      const key = `${x},${z}`;
      if (!cells.has(key)) cells.set(key, { x, z, block: "white_carpet", fold: false });
    }
  }
  return [...cells.values()];
}

/** The blown hatch cover lying in the grass a few blocks out from the hatch. */
export function hatchCover(frame) {
  const [hx, , hz] = frame.hatch;
  const [sx, sz] = frame.side;
  const [ax, az] = frame.axis;
  const out = [];
  for (let i = -1; i <= 1; i += 1) {
    for (let j = 0; j <= 1; j += 1) {
      out.push({
        x: Math.floor(hx + sx * (6 + j) + ax * (2 + i)),
        z: Math.floor(hz + sz * (6 + j) + az * (2 + i)),
        block: i === 0 && j === 0 ? "iron_block" : "iron_trapdoor",
      });
    }
  }
  return out;
}

/**
 * The ground graded level with the deck in front of the hatch, two wide and four out,
 * so a player can walk out whatever the terrain did there: per column, its new surface y.
 */
export function hatchApron(frame) {
  const [hx, , hz] = frame.hatch;
  const [sx, sz] = frame.side;
  const across = [Math.abs(sz), Math.abs(sx)]; // along the wall
  const out = [];
  for (let d = 0; d < 4; d += 1) {
    for (let w = 0; w < 2; w += 1) {
      out.push({ x: hx + sx * d + across[0] * w, z: hz + sz * d + across[1] * w, y: frame.deckY - 1 });
    }
  }
  return out;
}

/**
 * Where a player comes into the world: on the apron outside the hatch, or for a
 * splashdown on the deck inside, out of the sea. `y` is where their feet go.
 */
export function spawnPoint(frame) {
  const [hx, , hz] = frame.hatch;
  const [sx, sz] = frame.side;
  const d = frame.splash ? -2 : 2;
  return { x: Math.floor(hx + sx * d), y: frame.deckY, z: Math.floor(hz + sz * d) };
}

/**
 * Where the mothership hung over the pod in the signed-off scene: its minimum corner
 * for a pod placed at `frame`. Block coordinates are relative to each build's corner,
 * so a build's corner in the design was origin + offset.
 */
export function shipBase(frame, pod, ship) {
  return {
    x: frame.origin.x - pod.origin[0] + ship.origin[0] + ship.offset[0],
    y: frame.origin.y - pod.origin[1] + ship.origin[1] + ship.offset[1],
    z: frame.origin.z - pod.origin[2] + ship.origin[2] + ship.offset[2],
  };
}

/** If the base can't clear the ground there, these slides along its beam are tried. */
export const SHIP_SHIFTS = [0, 40, -40, 80, -80, 120, -120];

/**
 * The ship's height over ground whose highest block is `peak`: its signed-off height,
 * raised to clear the peak by `clear`, or null when that would push its top past the
 * build limit (`worldTop`, the highest buildable y).
 */
export function shipHeight(baseY, peak, sizeY, worldTop = 319, clear = 30) {
  const y = Math.max(baseY, peak + clear);
  return y + sizeY - 1 <= worldTop ? y : null;
}

/** The pod's starter kit, barrel by barrel (the pod has a few; extras stay empty). */
export const STARTER_KIT = [
  [["bread", 8], ["apple", 4], ["cooked_beef", 4]],
  [["stone_pickaxe", 1], ["stone_axe", 1], ["stone_sword", 1], ["stone_shovel", 1]],
  [["torch", 16], ["oak_log", 12], ["coal", 6]],
  [["white_bed", 1], ["water_bucket", 1], ["map", 1]],
];
