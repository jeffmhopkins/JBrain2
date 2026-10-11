// The Tricorder's and the Power Pack's rules (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M9,
// §M10, §DW1), kept free of @minecraft/* so node can test them; main.js does the reading
// and the writing.

// Dave's words, copied verbatim from the canon: Bleepbloop_City `notes/lore.md`, "Dave's
// lines". The lore page is the source: a line changes there first, then here.
// - Signed off by the owner (2026-10-11): every line here.
export const LINES = {
  noSignal: "…kssht… no signal…",
  notTuned: "…static… that one isn't tuned to you.",
  rebeamed:
    "…there you are. Without your Tricorder I nearly lost you. Here's a new one. Keep it close.",
  retired: "This tricorder is retired",
  burnt: (left) =>
    `Your Power Pack burnt out — you kept your things. ${
      left === 0 ? "No packs left." : left === 1 ? "1 pack left." : `${left} packs left.`
    }`,
};

export const TRICORDER = "jbrain:tricorder";
export const POWER_PACK = "jbrain:power_pack";
export const LORE = "Points where Dave says";

const DIMS = { overworld: "minecraft:overworld", nether: "minecraft:nether", the_end: "minecraft:the_end" };
const DIM_NAMES = { overworld: "the Overworld", nether: "the Nether", the_end: "the End" };

// Eight arrows, from the bearing to the target minus where the player faces. Bedrock's
// north is -z, so a bearing is atan2(dx, -dz) for the target and for the view alike.
export const ARROWS = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"];

export const arrowFor = (from, view, tx, tz) => {
  const target = Math.atan2(tx - from.x, -(tz - from.z));
  const facing = Math.atan2(view.x, -view.z);
  const turn = ((((target - facing) * 180) / Math.PI) % 360 + 360) % 360;
  return {
    arrow: ARROWS[Math.round(turn / 45) % 8],
    blocks: Math.round(Math.hypot(tx - from.x, tz - from.z)),
  };
};

// The actionbar line for an owner holding their Tricorder. `here` is the holder ({dim
// id, x, z}), `view` their view direction; with no waypoint it points at world spawn.
export const trackerLine = (wp, here, view, spawn) => {
  if (!wp) {
    const { arrow, blocks } = arrowFor(here, view, spawn.x, spawn.z);
    return `${arrow} World spawn · ${blocks} blocks`;
  }
  if (here.dim !== DIMS[wp.dim]) return `${wp.label} is in ${DIM_NAMES[wp.dim]}`;
  const { arrow, blocks } = arrowFor(here, view, wp.x, wp.z);
  const depth = wp.y === null || wp.y === undefined ? "" : ` · y ${wp.y}`;
  const there = blocks < 3 ? "You're here" : `${blocks} blocks`;
  return `${arrow} ${wp.label} · ${there}${depth}`;
};

// A waypoint from `scriptevent jb:waypoint {json}` (Dave's mc_set_waypoint): a record to
// store, or {clear: true}. Throws on anything malformed, which the pack logs as refused.
export const parseWaypoint = (message) => {
  const wp = JSON.parse(message);
  if (typeof wp !== "object" || wp === null) throw new Error("not an object");
  if (typeof wp.player !== "string" || !wp.player) throw new Error("no player");
  if (wp.clear) return { player: wp.player, clear: true };
  if (!Number.isFinite(wp.x) || !Number.isFinite(wp.z) || !(wp.dim in DIMS)) {
    throw new Error("x, z and dim are required");
  }
  return {
    player: wp.player,
    record: {
      x: wp.x,
      y: Number.isFinite(wp.y) ? wp.y : null,
      z: wp.z,
      dim: wp.dim,
      label: String(wp.label ?? "Waypoint").slice(0, 40),
    },
  };
};

// The wrapper's per-world settings, from `scriptevent jb:config {json}` at every start
// and whenever the owner changes one. Only known keys, only booleans.
export const parseConfig = (message) => {
  const raw = JSON.parse(message);
  if (typeof raw !== "object" || raw === null) throw new Error("not an object");
  const out = {};
  if (typeof raw.power_pack_keep === "boolean") out.power_pack_keep = raw.power_pack_keep;
  return out;
};

// Whose a Tricorder is, to the player examining it. An item records its owner's id and
// its serial; the world records each owner's current serial, so a re-beam retires every
// older copy wherever it is (a chest, the ground, a sibling's pocket) without finding it.
// No owner at all is a copy from before ownership (the home-test kit): retired.
export const tricorderStatus = (item, holderId, currentSerial) => {
  if (item.owner === undefined) return "retired";
  if (item.owner !== holderId) return "foreign";
  if (item.serial !== currentSerial) return "retired";
  return "own";
};

// What a player's death means for their things (§M10's fail-safe direction):
// - "vanilla": the switch is off (or the world rule is, so the game drops already);
// - "keep": a Power Pack AND their own Tricorder: one pack burns, everything stays;
// - "drop": the rule kept everything, so the pack drops it as a normal death would.
export const deathPlan = ({ switchOn, keepRule, hasPack, hasOwnTricorder }) => {
  if (!switchOn || !keepRule) return "vanilla";
  return hasPack && hasOwnTricorder ? "keep" : "drop";
};

// One stack on a "drop" death: keepOnDeath (the Tricorder) stays, Curse of Vanishing
// is destroyed, everything else falls.
export const slotFate = ({ keepOnDeath, vanishing }) => {
  if (keepOnDeath) return "stay";
  if (vanishing) return "destroy";
  return "drop";
};

// The pack to burn: the smallest stack, so a full stack stays whole; the first on a tie.
export const packToBurn = (stacks) => {
  let best;
  for (const s of stacks) if (!best || s.amount < best.amount) best = s;
  return best;
};

// Vanilla's XP drop on death: 7 per level, at most 100.
export const xpToDrop = (level) => Math.min(Math.max(0, Math.floor(level)) * 7, 100);

// Where a "drop" death's things go. A void death would drop them out of the world, so
// they go to the last place the player stood instead (§M10: a deliberate kindness).
export const dropSpot = (at, minY, lastSafe) => {
  if (at.y >= minY || !lastSafe || lastSafe.dim !== at.dim) return at;
  return lastSafe;
};
