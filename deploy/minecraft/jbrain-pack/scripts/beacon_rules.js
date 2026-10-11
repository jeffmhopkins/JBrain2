// The No-Spawn Beacon's rules (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M16; canon:
// Bleepbloop_City `notes/lore.md`, "The No-Spawn Beacon"), kept free of @minecraft/* so
// node can test them; beacon.js does the reading and the removing.

export const BEACON = "jbrain:no_spawn_beacon";
// An entity property: a player interacted with this mob, or it was bred, so it stays.
export const KEPT = "jb:kept";
// One world property per beacon, so a world can hold any number without one value
// outgrowing the property size limit.
export const RECORD_PREFIX = "jb:beacon:";

const CHUNK = 16;
// The beacon's own chunk and one on each side: 48 × 48 blocks.
const REACH = 1;

export const chunkOf = (v) => Math.floor(v / CHUNK);
export const chunkKey = (cx, cz) => `${cx},${cz}`;

// The 9 chunk keys a beacon at (x, z) covers. Math.floor, not truncation, so -1 is chunk
// -1 and the area is still centred on the beacon's own chunk at negative coordinates.
export const coveredChunks = ({ x, z }) => {
  const cx = chunkOf(x);
  const cz = chunkOf(z);
  const out = [];
  for (let dz = -REACH; dz <= REACH; dz++) {
    for (let dx = -REACH; dx <= REACH; dx++) out.push(chunkKey(cx + dx, cz + dz));
  }
  return out;
};

// The covered area as a corner and a size, for a volume query; full height is the
// dimension's to give.
export const areaOf = ({ x, z }) => {
  const side = (2 * REACH + 1) * CHUNK;
  return {
    x: (chunkOf(x) - REACH) * CHUNK,
    z: (chunkOf(z) - REACH) * CHUNK,
    size: side,
  };
};

const whole = (v) => Math.floor(v);

export const recordKey = (dim, at) =>
  `${RECORD_PREFIX}${dim}@${whole(at.x)},${whole(at.y)},${whole(at.z)}`;

export const makeRecord = (dim, at) => {
  const pos = { x: whole(at.x), y: whole(at.y), z: whole(at.z) };
  return { dim, ...pos, chunks: coveredChunks(pos) };
};

// A stored record, or undefined for anything unreadable (which the sweep then ignores).
export const parseRecord = (raw) => {
  if (typeof raw !== "string") return undefined;
  try {
    const r = JSON.parse(raw);
    if (typeof r?.dim !== "string") return undefined;
    if (![r.x, r.y, r.z].every(Number.isInteger)) return undefined;
    return makeRecord(r.dim, r);
  } catch {
    return undefined;
  }
};

// dimension id -> the chunk keys any beacon covers there. A Set: overlapping beacons
// cover a chunk once, and a spawn's check is one lookup.
export const buildIndex = (records) => {
  const index = new Map();
  for (const r of records) {
    if (!index.has(r.dim)) index.set(r.dim, new Set());
    for (const k of r.chunks) index.get(r.dim).add(k);
  }
  return index;
};

export const isProtected = (index, dim, { x, z }) =>
  Boolean(index.get(dim)?.has(chunkKey(chunkOf(x), chunkOf(z))));

// What the beacon removes, by type: hostile, neutral and passive mobs. A type not listed
// here is never touched (items, XP, arrows, boats, armor stands, another pack's
// entities), so a new mob the game adds is safe until it is listed.
export const HOSTILE = new Set([
  "blaze", "bogged", "breeze", "cave_spider", "creaking", "creeper", "drowned",
  "elder_guardian", "endermite", "evocation_illager", "ghast", "guardian", "hoglin", "husk",
  "magma_cube", "parched", "phantom", "piglin_brute", "pillager", "ravager", "shulker",
  "silverfish", "skeleton", "slime", "spider", "stray", "vex", "vindicator", "warden",
  "witch", "wither_skeleton", "zoglin", "zombie", "zombie_villager", "zombie_villager_v2",
].map((t) => `minecraft:${t}`));

export const NEUTRAL = new Set([
  "bee", "dolphin", "enderman", "goat", "llama", "panda", "piglin", "polar_bear",
  "trader_llama", "wolf", "zombie_pigman",
].map((t) => `minecraft:${t}`));

export const PASSIVE = new Set([
  "allay", "armadillo", "axolotl", "bat", "camel", "camel_husk", "cat", "chicken", "cod",
  "cow", "donkey", "fox", "frog", "glow_squid", "happy_ghast", "horse", "mooshroom", "mule",
  "nautilus", "ocelot", "parrot", "pig", "pufferfish", "rabbit", "salmon", "sheep",
  "skeleton_horse", "sniffer", "squid", "strider", "tadpole", "tropicalfish", "turtle",
  "zombie_horse", "zombie_nautilus",
].map((t) => `minecraft:${t}`));

export const REMOVABLE = new Set([...HOSTILE, ...NEUTRAL, ...PASSIVE]);

// Never removed, whatever their state: the crew, villagers (and the trader), the golems
// (canon calls the copper golems Old Maker helpers), and the bosses, so a beacon can't
// skip a fight. The Warden is not a boss here: it goes (owner, 2026-10-11).
export const ALWAYS_KEPT = new Set([
  "player", "villager", "villager_v2", "wandering_trader", "iron_golem", "snow_golem",
  "copper_golem", "ender_dragon", "wither",
].map((t) => `minecraft:${t}`));

// Why a mob stays, or null when the beacon removes it. `mob` is
// {typeId, tamed, named, leashed, kept}: kept is the KEPT property (interacted or bred).
export const keepReason = (mob) => {
  if (ALWAYS_KEPT.has(mob.typeId)) return "always kept";
  if (!REMOVABLE.has(mob.typeId)) return "not a mob the beacon removes";
  if (mob.tamed) return "tamed";
  if (mob.named) return "name tag";
  if (mob.leashed) return "on a lead";
  if (mob.kept) return "a player looked after it";
  return null;
};
