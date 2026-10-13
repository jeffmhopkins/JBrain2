// The wormhole gates' rules (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M12), kept free of
// @minecraft/* so node can test them; wormhole.js does the reading and the writing.
//
// A gate is a Nether-portal-shaped ring of blocks of diamond, 4 wide by 5 tall, standing
// along the x or the z axis. Its record names the ring's lowest corner (x, y, z), its axis
// and dimension, who planted it, and where it leads (`target`, null while dormant).

export const SEED = "jbrain:wormhole_seed";
export const SURFACE = "jbrain:wormhole";
export const FRAME_BLOCK = "minecraft:diamond_block";
export const AIR = "minecraft:air";
export const WIDTH = 4;
export const HEIGHT = 5;
// Opening and re-aiming are asked within this many blocks of the gate.
export const REACH = 4;
export const DIMS = {
  overworld: "minecraft:overworld",
  nether: "minecraft:nether",
  the_end: "minecraft:the_end",
};

// The players' messages. Not Dave's lines: the lore page says Dave's words for planting
// and opening come later (§M12 "Open"), so these stay plain status, never "[Dave]".
export const WORMHOLE_LINES = {
  planted: "The gate wakes, but stays dark. Ask Dave to open it to one of your saved places.",
  notAFrame:
    "The seed needs a ring of diamond blocks shaped like a Nether portal: 4 wide, 5 tall, empty inside.",
  taken: "That ring already holds a gate.",
  collapsed: "The gate collapses. Its seed burns out.",
  blocked: (place) => `The way to ${place} is blocked on the other side.`,
};

// The cells of a gate: the ten ring blocks it needs, the four optional corners, and the
// six interior cells its surface fills, lowest row first.
export const frameCells = ({ axis, x, y, z }) => {
  const at = (u, v) => (axis === "x" ? { x: x + u, y: y + v, z } : { x, y: y + v, z: z + u });
  const ring = [];
  const corners = [];
  const interior = [];
  for (let v = 0; v < HEIGHT; v++) {
    for (let u = 0; u < WIDTH; u++) {
      const edgeU = u === 0 || u === WIDTH - 1;
      const edgeV = v === 0 || v === HEIGHT - 1;
      if (edgeU && edgeV) corners.push(at(u, v));
      else if (edgeU || edgeV) ring.push(at(u, v));
      else interior.push(at(u, v));
    }
  }
  return { ring, corners, interior };
};

// The gate whose ring holds the block at `at`, or null. `read(x, y, z)` is that block's
// type id (undefined where it can't be read). Corners don't count: they are optional, so
// a gate can't be planted from one, and breaking one leaves the gate standing.
export const findFrame = (read, at) => {
  for (const axis of ["x", "z"]) {
    const along = axis === "x" ? at.x : at.z;
    for (let u0 = along - (WIDTH - 1); u0 <= along; u0++) {
      for (let y0 = at.y - (HEIGHT - 1); y0 <= at.y; y0++) {
        const gate = axis === "x" ? { axis, x: u0, y: y0, z: at.z } : { axis, x: at.x, y: y0, z: u0 };
        const { ring, interior } = frameCells(gate);
        if (!ring.some((c) => c.x === at.x && c.y === at.y && c.z === at.z)) continue;
        if (!ring.every((c) => read(c.x, c.y, c.z) === FRAME_BLOCK)) continue;
        if (!interior.every((c) => read(c.x, c.y, c.z) === AIR)) continue;
        return gate;
      }
    }
  }
  return null;
};

const same = (a, b) => a.x === b.x && a.y === b.y && a.z === b.z;

export const inRing = (gate, at) => frameCells(gate).ring.some((c) => same(c, at));
export const inInterior = (gate, at) => frameCells(gate).interior.some((c) => same(c, at));

// Two gates in one ring would fight over its surface.
export const overlaps = (a, b) =>
  a.dim === b.dim &&
  frameCells(a).interior.some((c) => frameCells(b).interior.some((d) => same(c, d)));

// How far a point is from the gate's 4x5 slab, in blocks (0 standing in it).
export const distanceTo = (gate, p) => {
  const span = (lo, hi, v) => (v < lo ? lo - v : v > hi ? v - hi : 0);
  const [x1, z1] = gate.axis === "x" ? [gate.x + WIDTH, gate.z + 1] : [gate.x + 1, gate.z + WIDTH];
  const dx = span(gate.x, x1, p.x);
  const dy = span(gate.y, gate.y + HEIGHT, p.y);
  const dz = span(gate.z, z1, p.z);
  return Math.hypot(dx, dy, dz);
};

// The gate a player may aim: their own, nearest, within REACH, in their dimension. When
// only someone else's is in reach, `theirs` says so, for a refusal that makes sense.
export const gateFor = (gates, who, dim, p) => {
  const near = gates
    .filter((g) => g.dim === dim && distanceTo(g, p) <= REACH)
    .sort((a, b) => distanceTo(a, p) - distanceTo(b, p));
  const mine = near.find((g) => g.planter.toLowerCase() === who.toLowerCase());
  return { gate: mine ?? null, theirs: !mine && near.length > 0 };
};

const NAME_MAX = 40;

// `scriptevent jb:wormhole {json}` from Dave's tools: aim a gate at a saved place, or
// forget a place its planter removed. Throws on anything malformed (refused, not guessed).
export const parseWormhole = (message) => {
  const raw = JSON.parse(message);
  if (typeof raw !== "object" || raw === null) throw new Error("not an object");
  if (typeof raw.player !== "string" || !raw.player) throw new Error("no player");
  const place = String(raw.place ?? "").slice(0, NAME_MAX);
  if (!place) throw new Error("no place");
  if (raw.op === "forget") return { op: "forget", player: raw.player, place };
  if (raw.op !== "aim") throw new Error("op must be aim or forget");
  const whole = [raw.x, raw.y, raw.z].every((n) => Number.isInteger(n));
  if (!whole || !(raw.dim in DIMS)) throw new Error("x, y, z and dim are required");
  return {
    op: "aim",
    player: raw.player,
    target: { place, x: raw.x, y: raw.y, z: raw.z, dim: raw.dim },
  };
};

// What aiming a gate at `target` does: "same" (already there, nothing charged), "free"
// (its first opening costs nothing beyond the seed), "charge" (a re-aim, one Power Pack)
// or "no_pack" (a re-aim with none to pay).
export const aimPlan = (gate, target, packs) => {
  const t = gate.target;
  if (t && t.place.toLowerCase() === target.place.toLowerCase() && same(t, target) && t.dim === target.dim) {
    return "same";
  }
  if (!gate.opened) return "free";
  return packs > 0 ? "charge" : "no_pack";
};

// Where a traveller lands: the middle of the saved block.
export const landing = (t) => ({ x: t.x + 0.5, y: t.y, z: t.z + 0.5 });

export const blockOf = (loc) => ({ x: Math.floor(loc.x), y: Math.floor(loc.y), z: Math.floor(loc.z) });

// A gate record from the world property; null when it can't be one (it is then ignored,
// never half-used).
export const parseGate = (raw) => {
  if (typeof raw !== "string") return null;
  try {
    const g = JSON.parse(raw);
    const ok =
      typeof g === "object" && g !== null && Number.isInteger(g.id) && (g.axis === "x" || g.axis === "z") &&
      [g.x, g.y, g.z].every(Number.isInteger) && g.dim in DIMS && typeof g.planter === "string";
    return ok ? { target: null, opened: false, ...g } : null;
  } catch {
    return null;
  }
};

const targetOf = (t) => (t ? { place: t.place, dim: t.dim, x: t.x, y: t.y, z: t.z } : null);

// The structured line the wrapper turns into an event for the map and Dave (§M12): a
// gate's corner, axis and dimension, its planter, where it leads, and `by`, who did this
// (the breaker on a collapse, empty when no player broke it).
export const gateEvent = (op, gate, by) => ({
  ev: "gate", op, id: gate.id, dim: gate.dim, x: gate.x, y: gate.y, z: gate.z,
  axis: gate.axis, planter: gate.planter, by, target: targetOf(gate.target),
});

// Every gate at once, sent after each load so a restore, an import or a missed line can't
// leave the map out of date.
export const gatesSnapshot = (gates) => ({
  ev: "gates",
  all: gates.map((g) => ({
    id: g.id, dim: g.dim, x: g.x, y: g.y, z: g.z, axis: g.axis, planter: g.planter,
    target: targetOf(g.target),
  })),
});
