// Transport on request's rules (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M10, "Teleport to a
// known place, one Power Pack per trip"), kept free of @minecraft/* so node can test them;
// transport.js does the reading, the form and the teleport.

export const DIMS = {
  overworld: "minecraft:overworld",
  nether: "minecraft:nether",
  the_end: "minecraft:the_end",
};
export const DIM_OF = Object.fromEntries(Object.entries(DIMS).map(([k, v]) => [v, k]));
const DIM_NAMES = { overworld: "Overworld", nether: "Nether", the_end: "End" };

// How long an offer waits for its Yes. Dave's mc_transport waits a little longer for the
// outcome, so a Yes after this can't move anyone the tool has stopped watching for.
export const OFFER_TTL_MS = 45_000;

const COORD_MAX = 30_000_000;
const ID = /^[0-9a-f]{8}$/;
const LAVA = new Set(["minecraft:lava", "minecraft:flowing_lava"]);

// The offer from `scriptevent jb:transport {json}` (Dave's mc_transport): the asker's own
// saved place, already looked up by the backend. Throws on anything malformed.
export const parseTransport = (message) => {
  const t = JSON.parse(message);
  if (typeof t !== "object" || t === null) throw new Error("not an object");
  if (typeof t.id !== "string" || !ID.test(t.id)) throw new Error("no id");
  if (typeof t.player !== "string" || !t.player || t.player.length > 32) {
    throw new Error("no player");
  }
  const whole = (v) => Number.isInteger(v) && Math.abs(v) <= COORD_MAX;
  if (!whole(t.x) || !whole(t.z) || !(t.dim in DIMS)) throw new Error("x, z and dim are required");
  if (t.y !== null && t.y !== undefined && !Number.isInteger(t.y)) throw new Error("bad y");
  const label = String(t.label ?? "").slice(0, 40).trim();
  if (!label) throw new Error("no label");
  return {
    id: t.id,
    player: t.player,
    label,
    x: t.x,
    y: Number.isInteger(t.y) ? t.y : null,
    z: t.z,
    dim: t.dim,
    visited: Array.isArray(t.visited) ? t.visited.filter((d) => d in DIMS) : [],
  };
};

// The wrapper's "Power Pack teleport" switch from `scriptevent jb:config {json}`, the same
// message that carries power_pack_keep. Only a boolean counts.
export const parseTransportSwitch = (message) => {
  const raw = JSON.parse(message);
  if (typeof raw !== "object" || raw === null) throw new Error("not an object");
  return typeof raw.power_pack_transport === "boolean" ? { on: raw.power_pack_transport } : {};
};

// The switch is on by default: only an explicit false turns it off.
export const switchIsOn = (stored) => stored !== false;

// A player's visited dimensions, from the world property; unreadable is none recorded yet.
export const parseVisited = (raw) => {
  if (typeof raw !== "string") return [];
  try {
    const list = JSON.parse(raw);
    return Array.isArray(list) ? list.filter((d) => d in DIMS) : [];
  } catch {
    return [];
  }
};

export const visitedAfter = (visited, dim) =>
  dim in DIMS && !visited.includes(dim) ? [...visited, dim].sort() : visited;

// Why a trip can't happen, or null. Asked when it's offered and again on the Yes, since
// the player can drop the pack or walk through a portal while the form is open.
// - "off": the world's switch; "no-tricorder": not their own one on them;
// - "no-pack": no Power Pack on them;
// - "unexplored": the place has no safe y, so it stays the Tricorder's only;
// - "unvisited": another dimension they've never been in (nobody gets into the End early).
export const refusal = ({ switchOn, tricorder, packs, hereDim, target, visited }) => {
  if (!switchOn) return "off";
  if (tricorder !== "own") return "no-tricorder";
  if (packs < 1) return "no-pack";
  if (target.y === null || target.y === undefined) return "unexplored";
  if (target.dim !== hereDim && !visited.includes(target.dim)) return "unvisited";
  return null;
};

const grouped = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");

// The form's body, the plan's wording: "Market — 1,240 blocks, Overworld. Use 1 Power
// Pack? (you have 3)". Another dimension has no distance worth giving.
export const offerText = ({ label, here, target, packs }) => {
  const where =
    here.dim === target.dim
      ? `${grouped(Math.round(Math.hypot(target.x - here.x, target.z - here.z)))} blocks, ${DIM_NAMES[target.dim]}`
      : DIM_NAMES[target.dim];
  return `${label} — ${where}. Use 1 Power Pack? (you have ${packs})`;
};

export const expired = (offeredAt, now) => now - offeredAt > OFFER_TTL_MS;

// The landing, from the two blocks the player would stand in (undefined: not loaded, so
// not reachable now; never generated is the same to a script). Collision is the
// teleport's own checkForBlocks; this only refuses what that would let through.
export const landing = (feet, head) => {
  if (!feet || !head) return "unloaded";
  if (LAVA.has(feet.typeId) || LAVA.has(head.typeId)) return "blocked";
  return null;
};
