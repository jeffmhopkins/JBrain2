// The Replicator's rules (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M15; canon: Bleepbloop_City
// `notes/lore.md`, "The Replicator"), kept free of @minecraft/* so node can test them;
// replicator.js does the reading and the writing.

export const REPLICATOR = "jbrain:replicator";
// The programmed one is its own item only so its slot can draw the lit sprite: an item's
// icon can't change per stack. Both place the same block.
export const PROGRAMMED = "jbrain:replicator_programmed";
export const POWER_PACK = "jbrain:power_pack";
export const STATE = "jbrain:programmed";
export const TARGET = "jb:target";

// Canon: 8 full stacks of the target is the pattern, so only a 64-stacker can be one.
export const FULL_STACK = 64;
export const PATTERN = 8 * FULL_STACK;
// One of the target about every 10 s: a vanilla furnace's 200 ticks per item, which is
// also the unit fuel burns in (coal's 1600 ticks lasts 8).
export const COOK_TICKS = 200;
// Canon: Dave explains it the first time someone has 8 Power Packs (§M19).
export const UNLOCK_PACKS = 8;

// The item's name, from its id: "minecraft:iron_ingot" is "Iron", as canon names an
// "Iron Replicator".
export const targetLabel = (id) =>
  String(id)
    .replace(/^[a-z0-9_]+:/, "")
    .replace(/_ingot$/, "")
    .split("_")
    .filter(Boolean)
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");

export const programmedName = (target) => `${targetLabel(target)} Replicator`;
export const programmedLore = (target) => `Burns any block into ${targetLabel(target).toLowerCase()}`;

// `scriptevent jb:replicator {"player": …, "target": …}` from Dave's mc_replicator_program.
// Vanilla items only: the Old Makers' own can't be copied, and none stacks to 64 anyway.
export const parseProgram = (message) => {
  const raw = JSON.parse(message);
  if (typeof raw !== "object" || raw === null) throw new Error("not an object");
  if (typeof raw.player !== "string" || !raw.player) throw new Error("no player");
  if (typeof raw.target !== "string" || !/^minecraft:[a-z0-9_.]{1,64}$/.test(raw.target)) {
    throw new Error("target must be a minecraft: item id");
  }
  return { player: raw.player, target: raw.target };
};

// Whether programming may go ahead, before anything is touched. `held` is the selected
// slot's {typeId, target}; `maxAmount` the target's stack size (undefined: no such item);
// `have` how many of it the player carries. A refusal is {code, detail} for Dave to word.
export const programCheck = ({ held, target, maxAmount, have }) => {
  if (held?.typeId !== REPLICATOR && held?.typeId !== PROGRAMMED) {
    return { code: "not_holding" };
  }
  if (maxAmount === undefined) return { code: "unknown_item" };
  if (maxAmount !== FULL_STACK) return { code: "stack", detail: maxAmount };
  if (held.target === target) return { code: "same" };
  if (have < PATTERN) return { code: "too_few", detail: have };
  return null;
};

// Which slots give up the pattern: exactly `n`, slot by slot. `slots` are
// {i, typeId, amount}; the result is [{i, left}], or null when there aren't `n`.
export const takePlan = (slots, target, n) => {
  const out = [];
  let need = n;
  for (const s of slots) {
    if (need === 0) break;
    if (s.typeId !== target) continue;
    const take = Math.min(s.amount, need);
    need -= take;
    out.push({ i: s.i, left: s.amount - take });
  }
  return need === 0 ? out : null;
};

// Vanilla Bedrock burn times in ticks. Nether wood doesn't burn, so its stems are out.
const FUEL = {
  "minecraft:coal": 1600,
  "minecraft:charcoal": 1600,
  "minecraft:coal_block": 16000,
  "minecraft:lava_bucket": 20000,
  "minecraft:blaze_rod": 2400,
  "minecraft:dried_kelp_block": 4000,
  "minecraft:bamboo_block": 300,
  "minecraft:bookshelf": 300,
  "minecraft:crafting_table": 300,
  "minecraft:chest": 300,
  "minecraft:stick": 100,
  "minecraft:bamboo": 50,
};
// What a spent fuel leaves in its slot.
export const FUEL_LEFTOVER = { "minecraft:lava_bucket": "minecraft:bucket" };

export const fuelTicks = (typeId) => {
  if (FUEL[typeId]) return FUEL[typeId];
  const id = String(typeId);
  if (/^minecraft:(crimson|warped)_/.test(id)) return 0;
  if (/^minecraft:[a-z_]+_(planks|log|wood)$/.test(id)) return 300;
  if (/^minecraft:[a-z_]+_slab$/.test(id) && /(oak|spruce|birch|jungle|acacia|mangrove|cherry|bamboo)_/.test(id)) {
    return 150;
  }
  return 0;
};

// Whether a container has room for one more of `target`: an empty slot, or a plain stack
// of it short of `maxAmount`. `slots` are {typeId, amount, named} or undefined.
export const roomFor = (slots, target, maxAmount) =>
  slots.some((s) => !s || (s.typeId === target && !s.named && s.amount < maxAmount));

// One ten-second turn. `rec` is the stored {target, fuel}; `input` whether a block waits
// on top, `burn` the ticks of the first fuel beside it (0 for none), `room` whether the
// output below can take one. The result says what to take and make, the fuel after, and
// the state the player is told when they look.
export const turn = (rec, { input, burn, room }) => {
  const idle = (state) => ({ state, takeInput: false, takeFuel: false, make: false, fuel: rec.fuel });
  if (!room) return idle("no_output");
  if (!input) return idle("no_input");
  let fuel = rec.fuel;
  let takeFuel = false;
  if (fuel < COOK_TICKS) {
    if (burn <= 0) return idle("no_fuel");
    fuel += burn;
    takeFuel = true;
  }
  return { state: "running", takeInput: true, takeFuel, make: true, fuel: fuel - COOK_TICKS };
};

// A placed replicator's record, in a world property per block, since a block's own state
// can't hold an item id.
export const recordKey = (dimId, { x, y, z }) =>
  `jb:replicator:${dimId.replace(/^minecraft:/, "")}:${x},${y},${z}`;

export const parseRecord = (raw) => {
  if (typeof raw !== "string") return undefined;
  try {
    const rec = JSON.parse(raw);
    if (typeof rec?.target !== "string") return undefined;
    return {
      target: rec.target,
      fuel: Number.isFinite(rec.fuel) ? Math.max(0, rec.fuel) : 0,
      state: typeof rec.state === "string" ? rec.state : "no_input",
    };
  } catch {
    return undefined;
  }
};

// What a player is shown when they use a placed replicator: what it makes, why it's
// waiting if it is, and where everything goes.
const WAITING = {
  running: "running",
  no_input: "waiting for a block on top",
  no_fuel: "waiting for fuel beside it",
  no_output: "waiting for room below",
};
export const HOW = "Blocks in a chest or hopper on top · fuel beside it · out into one below";

export const statusLine = (rec) => {
  if (!rec) return `§7Blank Replicator§r · ask Dave to program it`;
  const left = Math.floor(rec.fuel / COOK_TICKS);
  const fuel = left > 0 ? ` · fuel for ${left} more` : "";
  return `§6${programmedName(rec.target)}§r · ${WAITING[rec.state] ?? WAITING.no_input}${fuel}`;
};

// Whether a player carrying these stacks ({typeId, amount}) has unlocked the Replicator's
// guide entry: 8 Power Packs on them at once.
export const unlocksReplicator = (stacks) =>
  stacks.reduce((n, s) => n + (s.typeId === POWER_PACK ? s.amount : 0), 0) >= UNLOCK_PACKS;
