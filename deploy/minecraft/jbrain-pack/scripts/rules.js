// The Tricorder's and the Power Pack's rules (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M9,
// §M10, §DW1), kept free of @minecraft/* so node can test them; main.js does the reading
// and the writing.

// Dave's words, copied verbatim from the canon: Bleepbloop_City `notes/lore.md`, "Dave's
// lines". The lore page is the source: a line changes there first, then here.
// - Signed off by the owner: every line here (2026-10-11; powerPackLore 2026-10-12, the
//   item's lore: the technology that fuelled the Old Makers' ships, made now, not
//   salvaged). A rewording is one line: older lore is rewritten on the next look.
export const LINES = {
  powerPackLore: "The same fuel the Old Makers' ships ran on.",
  noSignal: "…kssht… no signal…",
  notTuned: "…static… that one isn't tuned to you.",
  rebeamed:
    "…there you are. Without your Tricorder I nearly lost you. Here's a new one. Keep it close.",
  retired: "This tricorder is retired",
  burnt: (left) =>
    `Your Power Pack burnt out — you kept your things. ${
      left === 0 ? "No packs left." : left === 1 ? "1 pack left." : `${left} packs left.`
    }`,
  // The welcome (about 40 s, private to the player). Two lines point at the pod and the
  // ship, so a world without the crash site gets the no-wreck pair instead.
  whereAmI: "…where am I?",
  whatsThis: "…what's this?",
  signal: "SIGNAL ACQUIRED",
  signalSub: "…a voice from orbit",
  welcome: (name, wreck) => [
    `…there you are, ${name}. You came down hard, but you're awake.`,
    "I'm Dave. I've watched over the Old Makers' ship for a very long time. I'm far above you, in orbit.",
    wreck
      ? "Look up. That ship is ours. She's hurt, but I can still reach into her, a little."
      : "Our ship came down long ago. I'm all that's still flying.",
    wreck
      ? "Your pod kept you alive. The barrels hold what I could save, and the bed is yours."
      : "This land has grown over everything we left. Keep your eyes open.",
    "That Tricorder is how I see you, and how I'll pull you home if you fall. Keep it on you.",
    "Need me? Type /dave and ask. Try: /dave where is the nearest village",
  ],
  firstDeath: [
    "…that was close. I pulled you out just in time.",
    "The transporter can only carry you, not your things. They're where you fell. Go and get them before they're gone.",
  ],
  firstEnderPearl:
    "…is that an Ender Pearl? The Old Makers ran on those. Ring eight of them around a Blaze Powder on a crafting table and you'll have a Power Pack, the same fuel our ships ran on. Bring me one when you do.",
  firstPowerPack: {
    keep: "A Power Pack! Keep it with your Tricorder. Next time you fall, it can hold a field around everything you carry, and I can bring it all home with you. It burns the pack, though.",
    transport:
      "And those places you've saved? Each one is a transport beacon. With a pack, I can bring you to any of them. Just ask.",
  },
  firstWormhole: "…a fold, held open. Stand close and tell me where it should lead.",
  firstReplicator:
    "Eight Power Packs… that's enough to wake a Replicator. Put a furnace in the middle of them on a crafting table, and I'll teach it to make one thing out of anything.",
};

export const TRICORDER = "jbrain:tricorder";
export const POWER_PACK = "jbrain:power_pack";

// The Tricorder carries no description: Bedrock's held-item pop-up shows an item's lore
// over the actionbar the heading is on, every time it's selected (owner, 2026-10-13). It
// says what the item does once a session on the actionbar instead, in plain words, not
// Dave's: it isn't one of his lines.
export const TRICORDER_HINT = "Your Tricorder points where Dave says · ask with /dave";
// About 3 s, then the heading.
export const TRICORDER_HINT_TICKS = 60;

// A Tricorder beamed before that still has its old line, cleared on the next look. A
// retired copy's "This tricorder is retired" is no description and stays.
export const tricorderLoreStale = (typeId, lore) =>
  typeId === TRICORDER && lore.length > 0 && !(lore.length === 1 && lore[0] === LINES.retired);

// A crafted Power Pack carries no lore (a recipe can't set one), so the pack writes it on
// every stack a player carries. Any lore other than exactly the current line is stale, so
// an old wording is replaced too. Bedrock stacks only identical lore, so every pack gets
// the same single line: lored stacks merge with each other again.
export const packLoreStale = (typeId, lore) =>
  typeId === POWER_PACK && (lore.length !== 1 || lore[0] !== LINES.powerPackLore);

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

// Within this many blocks across, the Tricorder reads "You're here".
export const HERE_BLOCKS = 3;

// The actionbar line for an owner holding their Tricorder. `here` is the holder ({dim
// id, x, z}), `view` their view direction; with no waypoint it points at world spawn.
export const trackerLine = (wp, here, view, spawn) => {
  if (!wp) {
    const { arrow, blocks } = arrowFor(here, view, spawn.x, spawn.z);
    return `${arrow} World spawn · ${blocks} blocks`;
  }
  if (here.dim !== DIMS[wp.dim]) {
    // Across dimensions it points at the nearest portal Dave knows on this side
    // (§M4e/§M9), still naming where the target is.
    const portal = nearestPortal(wp.via, here);
    if (!portal) return `${wp.label} is in ${DIM_NAMES[wp.dim]}`;
    const { arrow, blocks } = arrowFor(here, view, portal.x, portal.z);
    const there = blocks < 3 ? "You're here" : `${blocks} blocks`;
    return `${arrow} Nether portal · ${there} · ${wp.label} is in ${DIM_NAMES[wp.dim]}`;
  }
  const { arrow, blocks } = arrowFor(here, view, wp.x, wp.z);
  const depth = wp.y === null || wp.y === undefined ? "" : ` · y ${wp.y}`;
  const there = blocks < HERE_BLOCKS ? "You're here" : `${blocks} blocks`;
  return `${arrow} ${wp.label} · ${there}${depth}`;
};

// The portal in the holder's dimension nearest them, or undefined.
const nearestPortal = (via, here) => {
  let best;
  let bestGap = Infinity;
  for (const p of Array.isArray(via) ? via : []) {
    if (DIMS[p.dim] !== here.dim) continue;
    const gap = Math.hypot(p.x - here.x, p.z - here.z);
    if (gap < bestGap) [best, bestGap] = [p, gap];
  }
  return best;
};

// At most this many portals ride on a waypoint: Dave sends the nearest few.
export const VIA_MAX = 16;

// A waypoint's `via`: the known portals ({dim, x, z}) to point through from another
// dimension. Anything malformed is dropped, never the waypoint.
const parseVia = (raw) =>
  (Array.isArray(raw) ? raw : [])
    .filter((p) => p && Number.isFinite(p.x) && Number.isFinite(p.z) && p.dim in DIMS)
    .slice(0, VIA_MAX)
    .map((p) => ({ dim: p.dim, x: p.x, z: p.z }));

// The arrival chime rings again for the same waypoint only after the holder has walked
// this far back out, so pacing at the edge of "You're here" doesn't ring it each step.
export const CHIME_REARM_BLOCKS = 8;

// One waypoint, as the arrival chime remembers it: a new heading to the same spot under
// another name is a new arrival.
export const waypointId = (wp) => `${wp.dim} ${wp.x} ${wp.z} ${wp.label}`;

// The arrival chime (§M9) for a holder this frame: `heard` is the waypoint they last
// heard it for (waypointId, or undefined). Rings once on coming within HERE_BLOCKS, and
// forgets it past CHIME_REARM_BLOCKS; another dimension leaves it as it was.
export const arrivalChime = (wp, here, heard) => {
  if (!wp || here.dim !== DIMS[wp.dim]) return { ring: false, heard };
  const id = waypointId(wp);
  const blocks = Math.hypot(wp.x - here.x, wp.z - here.z);
  if (Math.round(blocks) < HERE_BLOCKS) return { ring: heard !== id, heard: id };
  if (blocks >= CHIME_REARM_BLOCKS && heard === id) return { ring: false, heard: undefined };
  return { ring: false, heard };
};

// A waypoint from `scriptevent jb:waypoint {json}` (Dave's mc_set_waypoint): a record to
// store, or {clear: true, endScan} (Dave's clear ends a live scan, §M25; the track
// drain's arrival clear doesn't). Throws on anything malformed, which the pack logs as
// refused.
export const parseWaypoint = (message) => {
  const wp = JSON.parse(message);
  if (typeof wp !== "object" || wp === null) throw new Error("not an object");
  if (typeof wp.player !== "string" || !wp.player) throw new Error("no player");
  if (wp.clear) return { player: wp.player, clear: true, endScan: Boolean(wp.end_scan) };
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
      via: parseVia(wp.via),
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

// Dave's item guide, unlocked as each item becomes craftable (§M19; canon: "Dave explains
// each item only once you can make it"). Like the game's recipe book, an unlock comes the
// first time the player holds the key ingredient. Only built items are here: the Tricorder
// from the start, the Power Pack on a first Ender Pearl, its uses on a first Power Pack,
// the No-Spawn Beacon on a first echo shard once they have made a Power Pack, and the
// Wormhole Seed on a first popped chorus fruit once they have made a Power Pack (§M12).
export const UNLOCKED_BY = {
  "minecraft:ender_pearl": "power_pack",
  [POWER_PACK]: "power_pack_uses",
  "minecraft:echo_shard": "no_spawn_beacon",
  "minecraft:popped_chorus_fruit": "wormhole_seed",
};
// An unlock that also needs an earlier one: an echo shard before any Power Pack teaches
// nothing, and the same shard held later (or with a pack in the same look) does. Popped
// chorus fruit and the Wormhole Seed likewise.
export const UNLOCK_NEEDS = { no_spawn_beacon: "power_pack_uses", wormhole_seed: "power_pack_uses" };

// A player's recorded unlocks, from the world property; anything unreadable is a fresh start.
export const parseKnown = (raw) => {
  if (typeof raw !== "string") return [];
  try {
    const list = JSON.parse(raw);
    return Array.isArray(list) ? list.filter((k) => typeof k === "string") : [];
  } catch {
    return [];
  }
};

// The unlocks after a look at what the player carries: never fewer than before, sorted
// so an unchanged set is recognisably the same.
export const unlocksFrom = (known, typeIds) => {
  const out = new Set(known);
  out.add("tricorder");
  const held = typeIds.map((t) => UNLOCKED_BY[t]).filter(Boolean);
  for (const k of held) if (!UNLOCK_NEEDS[k]) out.add(k);
  for (const k of held) if (UNLOCK_NEEDS[k] && out.has(UNLOCK_NEEDS[k])) out.add(k);
  return [...out].sort();
};

// The farthest a mob search reaches from the player: mobs only exist in the chunks
// around players, which a server's simulation distance keeps to about this.
export const FIND_MAX = 256;
const MOB_ID = /^(minecraft:)?[a-z_]{2,40}$/;

// A mob search from `scriptevent jb:find {json}` (Dave's mc_find_mob): whose
// surroundings, which mob type (namespaced), and how far out. Throws on anything malformed.
export const parseFind = (message) => {
  const q = JSON.parse(message);
  if (typeof q !== "object" || q === null) throw new Error("not an object");
  if (typeof q.player !== "string" || !q.player) throw new Error("no player");
  if (typeof q.type !== "string" || !MOB_ID.test(q.type)) throw new Error("not a mob type");
  const type = q.type.includes(":") ? q.type : `minecraft:${q.type}`;
  const max = Number.isFinite(q.max) ? Math.min(Math.max(Math.round(q.max), 8), FIND_MAX) : FIND_MAX;
  return { player: q.player, type, max };
};

// The console line answering a mob search. Its "at x= y= z=, " is the shape the
// backend's hit reader already parses for mc_find_block.
export const findLine = (q, who, here, mob) => {
  if (!mob) return `find none: no ${q.type} within ${q.max} blocks of ${who}`;
  const [x, y, z] = [mob.x, mob.y, mob.z].map(Math.floor);
  const blocks = Math.round(Math.hypot(mob.x - here.x, mob.z - here.z));
  const dim = Object.keys(DIMS).find((k) => DIMS[k] === here.dim) ?? here.dim;
  return `find found ${q.type} at x=${x} y=${y} z=${z}, ${blocks} blocks from ${who} (${dim})`;
};

// The Tricorder's short-range scan (§M23, lore "The Tricorder's short-range scan"): about
// 3 chunks around the player asking. Lines past ITEMS_LINES are left out, nearest kept.
export const ITEMS_MAX = 48;
export const ITEMS_LINES = 10;
const ITEM_ID = /^([a-z0-9_]{1,32}:)?[a-z0-9_]{2,64}$/;

// An item search from `scriptevent jb:items {json}` (Dave's mc_find_item): whose
// surroundings, which item types (1-8, namespaced), and how far out. Throws on anything
// malformed.
export const parseItems = (message) => {
  const q = JSON.parse(message);
  if (typeof q !== "object" || q === null) throw new Error("not an object");
  if (typeof q.player !== "string" || !q.player) throw new Error("no player");
  const types = Array.isArray(q.types) ? q.types : [];
  if (!types.length || types.length > 8 || !types.every((t) => typeof t === "string" && ITEM_ID.test(t))) {
    throw new Error("not 1-8 item types");
  }
  const max = Number.isFinite(q.max) ? Math.min(Math.max(Math.round(q.max), 8), ITEMS_MAX) : ITEMS_MAX;
  return { player: q.player, types: types.map((t) => (t.includes(":") ? t : `minecraft:${t}`)), max };
};

// The console lines answering an item search: one per kind per block, nearest first, in
// the "at x= y= z=, " shape the backend's hit reader parses; or one `items none` line.
export const itemsLines = (q, who, here, stacks) => {
  const dim = Object.keys(DIMS).find((k) => DIMS[k] === here.dim) ?? here.dim;
  const piles = new Map();
  for (const s of stacks) {
    const [x, y, z] = [s.location.x, s.location.y, s.location.z].map(Math.floor);
    const key = `${s.typeId} ${x} ${y} ${z}`;
    const pile = piles.get(key) ?? { typeId: s.typeId, x, y, z, amount: 0 };
    pile.amount += s.amount;
    piles.set(key, pile);
  }
  if (!piles.size) return [`items none: no ${q.types.join(", ")} on the ground within ${q.max} blocks of ${who}`];
  const away = (p) => Math.round(Math.hypot(p.x + 0.5 - here.x, p.z + 0.5 - here.z));
  return [...piles.values()]
    .sort((a, b) => away(a) - away(b))
    .slice(0, ITEMS_LINES)
    .map((p) => `items ground: ${p.amount} ${p.typeId} at x=${p.x} y=${p.y} z=${p.z}, ${away(p)} blocks from ${who} (${dim})`);
};

// A count of what a player carries, from `scriptevent jb:carry {json}` (Dave's
// mc_goal_progress, §M24): whose pockets and which item types (1-8, namespaced). Their own
// only — the backend sends the asker's name, never another's. Throws on anything malformed.
export const parseCarry = (message) => {
  const q = JSON.parse(message);
  if (typeof q !== "object" || q === null) throw new Error("not an object");
  if (typeof q.player !== "string" || !q.player) throw new Error("no player");
  const types = Array.isArray(q.types) ? q.types : [];
  if (!types.length || types.length > 8 || !types.every((t) => typeof t === "string" && ITEM_ID.test(t))) {
    throw new Error("not 1-8 item types");
  }
  return { player: q.player, types: types.map((t) => (t.includes(":") ? t : `minecraft:${t}`)) };
};

// Every shulker box colour's id ends so; "minecraft:shulker_box" is the undyed one.
export const isShulkerBox = (typeId) => typeId.endsWith("shulker_box");

// The console line answering a carry count: where they stand (so the backend can look
// around them from the app too), each asked type's total (0 when none), and how many
// shulker boxes they carry, whose contents stable 2.0.0 can't read.
export const carryLine = (q, who, here, stacks) => {
  const dim = Object.keys(DIMS).find((k) => DIMS[k] === here.dim) ?? here.dim;
  const counts = new Map(q.types.map((t) => [t, 0]));
  let boxes = 0;
  for (const s of stacks) {
    if (counts.has(s.typeId)) counts.set(s.typeId, counts.get(s.typeId) + s.amount);
    if (isShulkerBox(s.typeId)) boxes += s.amount;
  }
  const [x, y, z] = [here.x, here.y, here.z].map(Math.floor);
  const each = [...counts].map(([t, n]) => `${t}=${n}`).join(" ");
  return `carry counted for ${who} in ${dim} at x=${x} y=${y} z=${z}: ${each}; shulker boxes=${boxes}`;
};

// --- the Tricorder's live scan (§M25) ---------------------------------------------------
// The Tricorder's own short-range sense: while a scan is on, it finds the nearest block of
// one kind within SCAN_REACH blocks of its owner every SCAN_EVERY ticks. Free, and never
// over on its own: only a stop or a new waypoint ends it.
export const SCAN_REACH = 16; // one chunk each way: a 33×33×33 box
export const SCAN_EVERY = 200; // ticks: 10 s
const BLOCK_ID = /^([a-z0-9_]{1,32}:)?[a-z0-9_.]{1,64}$/;

// A scan from `scriptevent jb:scan {json}` (Dave's mc_tricorder_scan): {player, block} to
// start one, {player, stop: true} to end it. The block id is namespaced here; whether it
// names a real block is the game's to say (main.js asks BlockTypes). Throws on anything
// malformed.
export const parseScan = (message) => {
  const q = JSON.parse(message);
  if (typeof q !== "object" || q === null) throw new Error("not an object");
  if (typeof q.player !== "string" || !q.player) throw new Error("no player");
  if (q.stop) return { player: q.player, stop: true };
  const raw = typeof q.block === "string" ? q.block.trim().toLowerCase().replace(/ /g, "_") : "";
  if (!BLOCK_ID.test(raw)) throw new Error("not a block id");
  return { player: q.player, block: raw.includes(":") ? raw : `minecraft:${raw}` };
};

// The ids one scan looks for: an ore brings its deepslate twin (and the reverse), and a
// redstone ore its lit forms, as mc_find_block does. Candidates only: main.js keeps the
// ones the game knows, so "nether_gold_ore" gets no made-up deepslate twin.
export const scanTwins = (id) => {
  const [ns, name] = id.split(":");
  if (!name.endsWith("_ore")) return [id];
  const base = name.replace(/^lit_/, "").replace(/^deepslate_/, "");
  return [...new Set([id, ...[base, `deepslate_${base}`, `lit_${base}`, `lit_deepslate_${base}`].map((n) => `${ns}:${n}`)])];
};

// The block in plain words, as the Tricorder shows it: "minecraft:deepslate_diamond_ore"
// is "diamond", "minecraft:oak_log" is "oak log".
export const scanLabel = (id) => {
  let name = id.slice(id.indexOf(":") + 1);
  if (name.endsWith("_ore")) name = name.replace(/^lit_/, "").replace(/^deepslate_/, "").slice(0, -4);
  return name.replace(/_/g, " ");
};

// The box one scan reads around the player, its y clamped to the dimension (heightRange's
// max is exclusive).
export const scanBox = (at, height, reach = SCAN_REACH) => {
  const [x, y, z] = [at.x, at.y, at.z].map(Math.floor);
  const clampY = (v) => Math.min(Math.max(v, height.min), height.max - 1);
  return {
    from: { x: x - reach, y: clampY(y - reach), z: z - reach },
    to: { x: x + reach, y: clampY(y + reach), z: z + reach },
  };
};

export const inScanBox = (at, loc, reach = SCAN_REACH) =>
  ["x", "y", "z"].every((k) => Math.abs(loc[k] - Math.floor(at[k])) <= reach);

// The nearest block to the player by 3D distance to its centre; undefined for none.
export const nearestBlock = (at, locations) => {
  let best;
  let bestD = Infinity;
  for (const l of locations) {
    const d = Math.hypot(l.x + 0.5 - at.x, l.y + 0.5 - at.y, l.z + 0.5 - at.z);
    if (d < bestD) {
      best = { x: l.x, y: l.y, z: l.z };
      bestD = d;
    }
  }
  return best;
};

// Which scanning player's turn it is this frame: the most overdue, at most one a frame,
// so players who started together drift onto frames of their own and stay there.
export const pickScan = (due, frame) => {
  let pick;
  for (const [key, at] of due) if (at <= frame && (pick === undefined || at < due.get(pick))) pick = key;
  return pick;
};

// The scan as the map and Dave see it (the M26 pipe: pack line, sidecar, backend row).
// `set` when one starts, `found` / `none` when what it shows changes and again every
// SCAN_HEARTBEAT scans, so a steady target's "found 40 s ago" stays true, and `stop` when it
// ends. A `found` carries the block and its dimension; the rest carry none.
export const SCAN_HEARTBEAT = 6; // scans: a minute
const dimKey = (id) => Object.keys(DIMS).find((k) => DIMS[k] === id) ?? id;
export const scanEvent = (op, player, scan, target, dimId) => {
  const ev = { ev: "scan", op, player };
  if (op === "stop") return ev;
  Object.assign(ev, { label: scan.label, ids: scan.types });
  if (op === "found") Object.assign(ev, { dim: dimKey(dimId), x: target.x, y: target.y, z: target.z });
  return ev;
};

// Every kept scan after each load (labels only: the targets come with each one's first
// scan), so a restore, an import or a missed line can't leave the map out of date.
export const scansSnapshot = (scans) => ({
  ev: "scans",
  all: scans.map((s) => ({ player: s.player, label: s.label, ids: s.types })),
});

// The held Tricorder's line while a scan is on: the waypoint arrow to the last block
// found, or a line saying there's none within reach.
export const scanLine = (scan, target, here, view) => {
  const label = `Scanning: ${scan.label}`;
  if (!target) return `${label} · none nearby`;
  const dim = Object.keys(DIMS).find((k) => DIMS[k] === here.dim) ?? here.dim;
  // Aimed at the block's centre, so standing on it reads as here.
  return trackerLine({ x: target.x + 0.5, y: target.y, z: target.z + 0.5, dim, label }, here, view, undefined);
};
