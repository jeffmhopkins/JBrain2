// Each player's respawn point as the map shows it (docs/plans/MINECRAFT_BEDROCK_PLAN.md
// §M8, beds; the owner's pick docs/mocks/minecraft-map-layers/, variant B). Kept free of
// @minecraft/* so node can test it; spawnpoint.js does the reading.
//
// Stable 2.0.0 has no spawn-point-changed event, and in Bedrock a spawn point outlives
// its broken bed until the next death (review X4), so the kind is read from the block at
// the point every time, never assumed from how it was set.

// `unknown`: the blocks couldn't be read (an unloaded chunk) and this session hasn't seen
// the point. The box keeps the last kind it stored for a point, so it decides: the same
// point keeps its kind, a new one is `other`. Never `other` here — a rejoin far from home
// would report a good bed as gone.
export const KINDS = ["bed", "anchor", "other", "none", "unknown"];
// Bedrock has one bed block whatever its colour; the coloured ids are Java's, which a
// later Bedrock could take up.
const COLOURS = ["white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
  "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black"];
const BEDS = new Set(["minecraft:bed", ...COLOURS.map((c) => `minecraft:${c}_bed`)]);
const ANCHOR = "minecraft:respawn_anchor";
// The ten-second change check, in ticks.
export const CHECK_TICKS = 200;

export const DIMENSIONS = {
  "minecraft:overworld": "overworld",
  "minecraft:nether": "nether",
  "minecraft:the_end": "the_end",
};

export const blockKind = (typeId) => {
  if (typeof typeId !== "string") return null;
  if (typeId === ANCHOR) return "anchor";
  return BEDS.has(typeId) ? "bed" : null;
};

// Where to look for the block: the point itself first, then the blocks round it. Which
// block getSpawnPoint() names for a bed (the bed, or the spot beside it the player wakes
// on) is a home-test box check; a bed or anchor touching the point counts either way.
export const NEAR = (() => {
  const out = [[0, 0, 0]];
  for (const dy of [0, -1, 1]) {
    for (const dx of [-1, 0, 1]) {
      for (const dz of [-1, 0, 1]) if (dx || dy || dz) out.push([dx, dy, dz]);
    }
  }
  return out;
})();

// What stands at the point: "bed" or "anchor" when one is found, "other" when the blocks
// were read and none is (a /spawnpoint, or a bed broken since), and null when they
// couldn't be read (an unloaded chunk) — the caller then keeps what it last knew.
export const kindAt = (read) => {
  let unread = false;
  for (const [dx, dy, dz] of NEAR) {
    let typeId;
    try {
      typeId = read(dx, dy, dz);
    } catch {
      unread = true;
      continue;
    }
    if (typeId === undefined) {
      unread = true;
      continue;
    }
    const kind = blockKind(typeId);
    if (kind) return kind;
  }
  return unread ? null : "other";
};

// The structured line: whole blocks, a known dimension name, and no coordinates for none.
export const spawnLine = (player, point, kind) => {
  if (!point) return { ev: "spawnpoint", player, kind: "none" };
  return {
    ev: "spawnpoint",
    player,
    kind,
    dim: point.dim,
    x: Math.floor(point.x),
    y: Math.floor(point.y),
    z: Math.floor(point.z),
  };
};

// Two lines name the same spawn point and kind: only a change is sent.
export const sameLine = (a, b) =>
  !!a && !!b && a.kind === b.kind && a.dim === b.dim && a.x === b.x && a.y === b.y && a.z === b.z;

export const samePoint = (a, b) =>
  !!a && !!b && a.dim === b.dim && a.x === b.x && a.y === b.y && a.z === b.z;
