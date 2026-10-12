// Wormhole gates (docs/plans/MINECRAFT_BEDROCK_PLAN.md §M12; canon: Bleepbloop_City
// `notes/lore.md`, "Wormhole gates"): a Wormhole Seed planted in a ring of diamond blocks
// makes a dormant gate; Dave opens or re-aims it (`scriptevent jb:wormhole`) at one of its
// planter's saved places; anyone who steps into the surface goes there, one way. Stable
// @minecraft/server only. The rules themselves are in wormhole_rules.js.
import { system, world } from "@minecraft/server";
import { POWER_PACK, packToBurn } from "./rules.js";
import { AIR, DIMS, FRAME_BLOCK, SEED, SURFACE, WORMHOLE_LINES as L, aimPlan, blockOf,
  findFrame, frameCells, gateEvent, gateFor, gatesSnapshot, inInterior, inRing, landing,
  overlaps, parseGate, parseWormhole } from "./wormhole_rules.js";

// A plain line for the console, which Dave's tools read their answer from (as
// mc_set_waypoint reads `waypoint set`), and which is the trips' log.
const say = (msg) => console.log(`[jbrain] ${msg}`);
// The map's and Dave's record of a change, sent just before its prose line.
const emit = (ev) => console.log(`[jbrain] ${JSON.stringify(ev)}`);

const DIM_KEYS = Object.fromEntries(Object.entries(DIMS).map(([k, v]) => [v, k]));

// World properties: the list of gate ids, each gate's record, and the next id to give.
const INDEX = "jb:gates";
const gateKey = (id) => `jb:gate:${id}`;
const NEXT = "jb:gate_next";

const idsIn = (raw) => {
  try {
    const list = JSON.parse(String(raw ?? "[]"));
    return Array.isArray(list) ? list.filter(Number.isInteger) : [];
  } catch {
    return [];
  }
};
const ids = () => {
  try {
    return idsIn(world.getDynamicProperty(INDEX));
  } catch {
    return [];
  }
};

const gatesOf = (list) => list.map((id) => parseGate(world.getDynamicProperty(gateKey(id)))).filter(Boolean);
const allGates = () => gatesOf(ids());
// The load snapshot replaces every gate the map holds, so a failed read must throw rather
// than pass for "no gates".
const allGatesOrThrow = () => gatesOf(idsIn(world.getDynamicProperty(INDEX)));

const save = (gate) => {
  world.setDynamicProperty(gateKey(gate.id), JSON.stringify(gate));
  const list = ids();
  if (!list.includes(gate.id)) world.setDynamicProperty(INDEX, JSON.stringify([...list, gate.id]));
};

const drop = (gate) => {
  world.setDynamicProperty(gateKey(gate.id), undefined);
  world.setDynamicProperty(INDEX, JSON.stringify(ids().filter((i) => i !== gate.id)));
};

// A block's type id, or undefined where it can't be read (an unloaded chunk, off the
// world): a gate far from everyone is left alone, never judged broken.
const typeAt = (dim, at) => {
  try {
    return dim.getBlock(at)?.typeId;
  } catch {
    return undefined;
  }
};

const setAt = (dim, at, type) => dim.getBlock(at)?.setType(type);

const dimOf = (gate) => world.getDimension(DIMS[gate.dim]);
const where = (gate) => `${gate.x} ${gate.y} ${gate.z} (${gate.dim})`;

// Sound and sparkle are only dressing: a failure in either never stops what they dress.
const fx = (fn) => {
  try {
    fn();
  } catch {
    // An unknown particle or sound id on this version is not worth a refusal.
  }
};

const fill = (gate, type) => {
  const dim = dimOf(gate);
  for (const c of frameCells(gate).interior) {
    const now = typeAt(dim, c);
    if (now === AIR || now === SURFACE) setAt(dim, c, type);
  }
};

const named = (name) => world.getAllPlayers().find((p) => p.name.toLowerCase() === name.toLowerCase());

// --- planting ---------------------------------------------------------------------------

// What else happens on a planting: Dave's first-gate beat (welcome.js), set by init.
let onPlanted = () => {};

const plant = (player, block) => {
  const dim = player.dimension;
  const dimKey = DIM_KEYS[dim.id];
  const ring = block.typeId === FRAME_BLOCK && dimKey
    ? findFrame((x, y, z) => typeAt(dim, { x, y, z }), block.location)
    : null;
  if (!ring) {
    player.sendMessage(`§7${L.notAFrame}`);
    return;
  }
  const gate = { ...ring, dim: dimKey };
  if (allGates().some((g) => overlaps(g, gate))) {
    player.sendMessage(`§7${L.taken}`);
    return;
  }
  const inv = player.getComponent("minecraft:inventory")?.container;
  const slot = player.selectedSlotIndex;
  if (inv?.getItem(slot)?.typeId !== SEED) return;
  inv.setItem(slot, undefined); // stack size 1: the seed is gone into the gate
  const id = (Number(world.getDynamicProperty(NEXT)) || 0) + 1;
  world.setDynamicProperty(NEXT, id);
  const made = { id, ...gate, planter: player.name, planterId: player.id, target: null, opened: false };
  save(made);
  player.sendMessage(`§7${L.planted}`);
  fx(() => player.playSound("block.end_portal_frame.fill"));
  fx(() => {
    for (const c of frameCells(gate).interior) {
      dim.spawnParticle("minecraft:portal_reverse_particle", { x: c.x + 0.5, y: c.y + 0.5, z: c.z + 0.5 });
    }
  });
  emit(gateEvent("planted", made, player.name));
  say(`wormhole planted: gate ${id} by ${player.name} at ${where({ ...gate, id })}`);
  fx(() => onPlanted(player));
};

// --- breaking ---------------------------------------------------------------------------

// The fold collapses and the seed burns out (owner, 2026-10-11): the record goes, the
// surface goes, and nothing drops. A new seed and a whole ring make a new gate.
const collapse = (gate, by) => {
  drop(gate);
  fx(() => fill(gate, AIR));
  for (const p of [by, named(gate.planter)]) {
    if (p) fx(() => p.sendMessage(`§7${L.collapsed}`));
  }
  const breaker = by ? `, broken by ${by.name}` : "";
  emit(gateEvent("collapsed", gate, by?.name ?? ""));
  say(`wormhole collapsed: gate ${gate.id} (${gate.planter}'s) at ${where(gate)}${breaker}`);
};

const onBreak = (ev) => {
  if (ev.brokenBlockPermutation?.type?.id !== FRAME_BLOCK) return;
  const dimKey = DIM_KEYS[ev.dimension?.id ?? ev.block.dimension.id];
  const at = ev.block.location;
  for (const gate of allGates()) {
    if (gate.dim === dimKey && inRing(gate, at)) collapse(gate, ev.player);
  }
};

// Pistons, explosions and commands break a ring without a player's break event; a
// once-a-second look at every gate in loaded ground catches those, and puts back any
// surface cell a flood or a falling block cleared.
const sweep = () => {
  for (const gate of allGates()) {
    const dim = dimOf(gate);
    const types = frameCells(gate).ring.map((c) => typeAt(dim, c));
    if (types.some((t) => t !== undefined && t !== FRAME_BLOCK)) {
      collapse(gate, null);
    } else if (gate.target) {
      fill(gate, SURFACE);
    }
  }
};

// --- Dave's opening and re-aiming -------------------------------------------------------

const packsOn = (player) => {
  const out = [];
  const inv = player.getComponent("minecraft:inventory")?.container;
  if (inv) {
    for (let i = 0; i < inv.size; i++) {
      const item = inv.getItem(i);
      if (item?.typeId === POWER_PACK) out.push({ item, put: (v) => inv.setItem(i, v) });
    }
  }
  const eq = player.getComponent("minecraft:equippable");
  const off = eq?.getEquipment("Offhand");
  if (off?.typeId === POWER_PACK) out.push({ item: off, put: (v) => eq.setEquipment("Offhand", v) });
  return out;
};

const burnOne = (packs) => {
  const pick = packToBurn(packs.map((p, i) => ({ i, amount: p.item.amount })));
  const { item, put } = packs[pick.i];
  if (item.amount > 1) {
    item.amount -= 1;
    put(item);
  } else {
    put(undefined);
  }
};

const aim = ({ player: name, target }) => {
  const player = named(name);
  if (!player) return say(`wormhole refused: ${name} isn't in the world right now`);
  const dimKey = DIM_KEYS[player.dimension.id];
  const { gate, theirs } = gateFor(allGates(), player.name, dimKey, player.location);
  if (!gate) {
    return say(
      theirs
        ? `wormhole refused: the gate near ${player.name} isn't theirs; only the one who`
          + " planted a gate can open or re-aim it"
        : `wormhole refused: ${player.name} isn't within 4 blocks of a gate they planted`,
    );
  }
  const dim = dimOf(gate);
  if (frameCells(gate).interior.some((c) => ![AIR, SURFACE].includes(typeAt(dim, c)))) {
    return say(`wormhole refused: something is inside gate ${gate.id}'s ring; clear it first`);
  }
  const packs = packsOn(player);
  const have = packs.reduce((n, p) => n + p.item.amount, 0);
  const plan = aimPlan(gate, target, have);
  if (plan === "same") return say(`wormhole unchanged: gate ${gate.id} already leads to ${target.place}`);
  if (plan === "no_pack") {
    return say(`wormhole refused: re-aiming gate ${gate.id} costs a Power Pack, and ${player.name} has none`);
  }
  fill(gate, SURFACE);
  const aimed = { ...gate, target, opened: true };
  save(aimed);
  emit(gateEvent(plan === "charge" ? "aimed" : "opened", aimed, player.name));
  // Only now has the gate really changed, so only now is the pack spent.
  fx(() => player.playSound("mob.endermen.portal"));
  const to = `${target.place} at ${target.x} ${target.y} ${target.z} (${target.dim})`;
  if (plan === "charge") {
    burnOne(packs);
    return say(`wormhole aimed: gate ${gate.id} now leads to ${to}; 1 Power Pack used, ${have - 1} left`);
  }
  return say(`wormhole opened: gate ${gate.id} leads to ${to} (its first opening is free)`);
};

// A place its planter removed takes their gates that led there dormant (§M12).
const forget = ({ player, place }) => {
  let n = 0;
  for (const gate of allGates()) {
    const t = gate.target;
    const theirs = gate.planter.toLowerCase() === player.toLowerCase();
    if (!theirs || t?.place.toLowerCase() !== place.toLowerCase()) continue;
    fx(() => fill(gate, AIR));
    const dark = { ...gate, target: null };
    save(dark);
    emit(gateEvent("dormant", dark, gate.planter));
    n += 1;
  }
  say(`wormhole forgot ${place} for ${player}: ${n} gate(s) went dormant`);
};

// --- stepping through -------------------------------------------------------------------

// Who stood in a surface last look: a trip happens on stepping in, so a refused one
// isn't retried, and re-told, four times a second while they stand there.
const inside = new Set();

const travel = (player, gate) => {
  const t = gate.target;
  if (!t) return;
  const dimension = world.getDimension(DIMS[t.dim]);
  const ok = player.tryTeleport(landing(t), { dimension, checkForBlocks: true });
  const trip = `${player.name} through gate ${gate.id} (${gate.planter}'s) to ${t.place}`
    + ` at ${t.x} ${t.y} ${t.z} (${t.dim})`;
  if (ok) {
    fx(() => player.playSound("mob.endermen.portal"));
    say(`wormhole trip: ${trip}`);
  } else {
    player.sendMessage(`§7${L.blocked(t.place)}`);
    say(`wormhole trip refused, the landing is blocked: ${trip}`);
  }
};

const look = () => {
  const gates = allGates();
  for (const player of world.getAllPlayers()) {
    try {
      const cell = blockOf(player.location);
      if (typeAt(player.dimension, cell) !== SURFACE) {
        inside.delete(player.id);
        continue;
      }
      if (inside.has(player.id)) continue;
      inside.add(player.id);
      const dimKey = DIM_KEYS[player.dimension.id];
      const gate = gates.find((g) => g.dim === dimKey && inInterior(g, cell));
      // A surface no gate owns is left over from one that is gone.
      if (gate) travel(player, gate);
      else setAt(player.dimension, cell, AIR);
    } catch (e) {
      say(`wormhole failed for ${player.name}: ${e}`);
    }
  }
};

export const initWormholes = ({ onPlanted: planted } = {}) => {
  if (planted) onPlanted = planted;
  world.afterEvents.playerInteractWithBlock.subscribe((ev) => {
    const item = ev.beforeItemStack ?? ev.itemStack;
    if (item?.typeId !== SEED || ev.isFirstEvent === false) return;
    try {
      plant(ev.player, ev.block);
    } catch (e) {
      say(`wormhole planting failed for ${ev.player.name}: ${e}`);
    }
  });
  world.afterEvents.playerBreakBlock.subscribe((ev) => {
    try {
      onBreak(ev);
    } catch (e) {
      say(`wormhole break check failed: ${e}`);
    }
  });
  system.afterEvents.scriptEventReceive.subscribe((ev) => {
    if (ev.id !== "jb:wormhole") return;
    try {
      const cmd = parseWormhole(ev.message);
      if (cmd.op === "aim") aim(cmd);
      else forget(cmd);
    } catch (e) {
      say(`wormhole refused: ${e}`);
    }
  });
  let frame = 0;
  let reported = false;
  system.runInterval(() => {
    // Once per load, after the first tick: the properties can't be read any sooner.
    if (!reported) {
      try {
        emit(gatesSnapshot(allGatesOrThrow()));
        reported = true;
      } catch (e) {
        say(`wormhole report failed, trying again: ${e}`);
      }
    }
    look();
    frame = (frame + 1) % 4;
    if (frame === 0) {
      try {
        sweep();
      } catch (e) {
        say(`wormhole sweep failed: ${e}`);
      }
    }
  }, 5);
};
