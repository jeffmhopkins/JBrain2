// node --test: the jbrain pack's wormhole gates (wormhole.js, through main.js) against the
// fake server, given blocks, dimensions and teleporting here so the shared fake stays
// small. The supervisor's test_minecraft_wormhole.py lays the pack out beside it.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { Dimension, ItemStack, Player, sim, world } from "@minecraft/server";
import { SURFACE, WORMHOLE_LINES as L, frameCells } from "./pack/scripts/wormhole_rules.js";

// --- blocks, dimensions and teleports for the fake ---------------------------------------
const AIR = "minecraft:air";
const DIAMOND = "minecraft:diamond_block";
const key = ({ x, y, z }) => `${x},${y},${z}`;
const cellsOf = (dim) => (dim.cells ??= new Map());
Dimension.prototype.getBlock = function (at) {
  const loc = { x: Math.floor(at.x), y: Math.floor(at.y), z: Math.floor(at.z) };
  if (this.unloadedX !== undefined && loc.x >= this.unloadedX) throw new Error("LocationInUnloadedChunkError");
  const cells = cellsOf(this);
  return {
    location: loc,
    dimension: this,
    get typeId() {
      return cells.get(key(loc)) ?? AIR;
    },
    setType(t) {
      if (t === AIR) cells.delete(key(loc));
      else cells.set(key(loc), t);
    },
  };
};
let nether = new Dimension("minecraft:nether");
world.getDimension = (id) => (id === "minecraft:nether" ? nether : sim.overworld);
// The game's checkForBlocks: refused where the body (feet and head) would be in a block.
Player.prototype.tryTeleport = function (loc, opts) {
  const dim = opts?.dimension ?? this.dimension;
  if (opts?.checkForBlocks) {
    for (const dy of [0, 1]) if (dim.getBlock({ ...loc, y: loc.y + dy }).typeId !== AIR) return false;
  }
  this.location = { ...loc };
  this.dimension = dim;
  return true;
};

await import("./pack/scripts/main.js");

const set = (dim, at, t) => dim.getBlock(at).setType(t);
const typeAt = (dim, at) => dim.getBlock(at).typeId;
const lines = (prefix) => sim.logs.filter((l) => l.startsWith(`[jbrain] ${prefix}`));
const last = () => sim.logs.at(-1);

const GATE = { axis: "x", x: 0, y: 64, z: 0 };
const build = (gate = GATE, { corners = false, dim = sim.overworld } = {}) => {
  const c = frameCells(gate);
  for (const b of [...c.ring, ...(corners ? c.corners : [])]) set(dim, b, DIAMOND);
  return c;
};

const holding = (p, typeId, amount = 1) => {
  p.selectedSlotIndex = 0;
  p.inventory.setItem(0, new ItemStack(typeId, amount));
};
const plant = (p, at) => {
  const block = p.dimension.getBlock(at);
  const item = p.inventory.getItem(p.selectedSlotIndex);
  sim.interact({ player: p, block, itemStack: item, beforeItemStack: item, isFirstEvent: true });
};
const crew = (name, id, at = { x: 1.5, y: 64, z: 3.5 }) => {
  const p = sim.player(name, id);
  p.location = { ...at };
  return p;
};
const aim = (player, place, to) =>
  sim.scriptevent("jb:wormhole", JSON.stringify({ op: "aim", player, place, dim: "overworld", ...to }));
const surface = (cells, dim = sim.overworld) => cells.interior.map((c) => typeAt(dim, c));
const packs = (p) =>
  p.inventory.slots.filter((s) => s?.typeId === "jbrain:power_pack").reduce((n, s) => n + s.amount, 0);
const gates = () => JSON.parse(sim.props.get("jb:gates") ?? "[]");
const record = (id) => JSON.parse(sim.props.get(`jb:gate:${id}`));
const step = (p, at) => {
  p.location = { ...at };
  sim.tick();
};

beforeEach(() => {
  sim.reset();
  nether = new Dimension("minecraft:nether");
});

// A planted gate, opened by its planter to "Tree Farm" at 100 70 -50.
const openGate = () => {
  const cells = build();
  const mira = crew("Mira", "-1");
  holding(mira, "jbrain:wormhole_seed");
  plant(mira, cells.ring[0]);
  aim("Mira", "Tree Farm", { x: 100, y: 70, z: -50 });
  return { cells, mira };
};

test("planting the seed in a ring of diamond blocks: the seed goes, a dormant gate stays", () => {
  const cells = build();
  const mira = crew("Mira", "-1");
  holding(mira, "jbrain:wormhole_seed");
  plant(mira, cells.ring[5]);
  assert.equal(mira.inventory.slots[0], undefined);
  assert.deepEqual(mira.messages, [`§7${L.planted}`]);
  assert.deepEqual(gates(), [1]);
  assert.deepEqual(record(1), {
    id: 1,
    ...GATE,
    dim: "overworld",
    planter: "Mira",
    planterId: "-1",
    target: null,
    opened: false,
  });
  assert.deepEqual(surface(cells), Array(6).fill(AIR)); // dormant: dark
  assert.equal(last(), "[jbrain] wormhole planted: gate 1 by Mira at 0 64 0 (overworld)");
});

test("either axis, with or without corners, from a top or a side block", () => {
  const along = { axis: "z", x: 20, y: 70, z: 5 };
  const cells = build(along, { corners: true });
  const p = crew("Alex", "-2");
  holding(p, "jbrain:wormhole_seed");
  plant(p, cells.ring.at(-1)); // the top
  assert.equal(record(1).axis, "z");
  build();
  holding(p, "jbrain:wormhole_seed");
  plant(p, frameCells(GATE).ring.find((c) => c.y === 66)); // a side
  assert.deepEqual(gates(), [1, 2]);
});

test("no gate from a wrong ring, a filled one, a corner, or a ring already holding one", () => {
  const cells = build();
  const p = crew("Steve", "-3");
  holding(p, "jbrain:wormhole_seed");
  set(sim.overworld, cells.ring[0], "minecraft:gold_block");
  plant(p, cells.ring[3]);
  set(sim.overworld, cells.ring[0], DIAMOND);
  set(sim.overworld, cells.interior[2], "minecraft:torch");
  plant(p, cells.ring[3]);
  set(sim.overworld, cells.interior[2], AIR);
  set(sim.overworld, cells.corners[0], DIAMOND);
  plant(p, cells.corners[0]);
  plant(p, { x: 50, y: 64, z: 50 }); // plain ground
  assert.deepEqual(p.messages, Array(4).fill(`§7${L.notAFrame}`));
  assert.equal(p.inventory.slots[0].typeId, "jbrain:wormhole_seed"); // kept every time
  plant(p, cells.ring[3]);
  holding(p, "jbrain:wormhole_seed");
  plant(p, cells.ring[4]);
  assert.equal(p.messages.at(-1), `§7${L.taken}`);
  assert.equal(p.inventory.slots[0].typeId, "jbrain:wormhole_seed");
  assert.deepEqual(gates(), [1]);
});

test("anything else used on the ring does nothing", () => {
  const cells = build();
  const p = crew("Steve", "-3");
  holding(p, "jbrain:power_pack");
  plant(p, cells.ring[0]);
  assert.deepEqual(gates(), []);
  assert.deepEqual(p.messages, []);
});

test("Dave opens it for its planter: free the first time, the surface fills", () => {
  const cells = build();
  const mira = crew("Mira", "-1");
  holding(mira, "jbrain:wormhole_seed");
  plant(mira, cells.ring[0]);
  mira.inventory.setItem(3, new ItemStack("jbrain:power_pack", 2));
  aim("mira", "Tree Farm", { x: 100, y: 70, z: -50 });
  assert.equal(last(), "[jbrain] wormhole opened: gate 1 leads to Tree Farm at 100 70 -50 (overworld) (its first opening is free)");
  assert.deepEqual(surface(cells), Array(6).fill(SURFACE));
  assert.equal(packs(mira), 2);
  assert.deepEqual(record(1).target, { place: "Tree Farm", x: 100, y: 70, z: -50, dim: "overworld" });
});

test("opening is refused offline, out of reach, at someone else's gate, or with the ring filled", () => {
  const { cells } = openGate();
  const before = record(1);
  aim("Nobody", "Base", { x: 1, y: 64, z: 1 });
  assert.equal(last(), "[jbrain] wormhole refused: Nobody isn't in the world right now");
  const steve = crew("Steve", "-3");
  steve.inventory.setItem(0, new ItemStack("jbrain:power_pack", 5));
  aim("Steve", "Base", { x: 1, y: 64, z: 1 });
  assert.match(last(), /the gate near Steve isn't theirs; only the one who planted a gate can open or re-aim it$/);
  const mira = world.getAllPlayers().find((p) => p.name === "Mira");
  mira.location = { x: 1.5, y: 64, z: 5.5 }; // 4.5 blocks off the slab
  aim("Mira", "Base", { x: 1, y: 64, z: 1 });
  assert.equal(last(), "[jbrain] wormhole refused: Mira isn't within 4 blocks of a gate they planted");
  mira.location = { x: 1.5, y: 64, z: 3 };
  mira.inventory.setItem(0, new ItemStack("jbrain:power_pack", 1));
  set(sim.overworld, cells.interior[0], "minecraft:dirt");
  aim("Mira", "Base", { x: 1, y: 64, z: 1 });
  assert.equal(last(), "[jbrain] wormhole refused: something is inside gate 1's ring; clear it first");
  assert.deepEqual(record(1), before);
  assert.equal(packs(mira), 1);
  assert.equal(packs(steve), 5);
});

test("a re-aim costs one Power Pack, taken only once the gate has really changed", () => {
  const { cells, mira } = openGate();
  aim("Mira", "Mine", { x: 5, y: -40, z: 5 });
  assert.equal(last(), "[jbrain] wormhole refused: re-aiming gate 1 costs a Power Pack, and Mira has none");
  assert.equal(record(1).target.place, "Tree Farm"); // kept its target
  mira.inventory.setItem(4, new ItemStack("jbrain:power_pack", 3));
  mira.getComponent("minecraft:equippable").setEquipment("Offhand", new ItemStack("jbrain:power_pack", 1));
  aim("Mira", "tree farm", { x: 100, y: 70, z: -50 });
  assert.equal(last(), "[jbrain] wormhole unchanged: gate 1 already leads to tree farm");
  assert.equal(packs(mira) + mira.equipment.Offhand.amount, 4);
  aim("Mira", "Mine", { x: 5, y: -40, z: 5 });
  assert.equal(last(), "[jbrain] wormhole aimed: gate 1 now leads to Mine at 5 -40 5 (overworld); 1 Power Pack used, 3 left");
  assert.equal(mira.equipment.Offhand, undefined); // the smallest stack first
  assert.equal(packs(mira), 3);
  assert.equal(record(1).target.place, "Mine");
  assert.deepEqual(surface(cells), Array(6).fill(SURFACE));
  // A broken ring can't be re-aimed, so nothing is charged for it.
  set(sim.overworld, cells.ring[0], AIR);
  sim.breakBlock({
    player: mira,
    block: sim.overworld.getBlock(cells.ring[0]),
    brokenBlockPermutation: { type: { id: DIAMOND } },
    dimension: sim.overworld,
  });
  aim("Mira", "Base", { x: 1, y: 64, z: 1 });
  assert.match(last(), /refused/);
  assert.equal(packs(mira), 3);
});

test("anyone may step through, one way, and the trip is logged", () => {
  const { cells } = openGate();
  const sib = crew("Sib", "-9", { x: 9, y: 64, z: 9 });
  sim.tick();
  step(sib, { x: 1.5, y: 65, z: 0.5 }); // into the surface
  assert.deepEqual(sib.location, { x: 100.5, y: 70, z: -49.5 });
  assert.equal(last(), "[jbrain] wormhole trip: Sib through gate 1 (Mira's) to Tree Farm at 100 70 -50 (overworld)");
  // Nothing comes back: the other end is just the saved place.
  assert.equal(typeAt(sim.overworld, { x: 100, y: 70, z: -50 }), AIR);
  assert.deepEqual(surface(cells), Array(6).fill(SURFACE));
});

test("a gate can lead to another dimension", () => {
  const cells = build();
  const mira = crew("Mira", "-1");
  holding(mira, "jbrain:wormhole_seed");
  plant(mira, cells.ring[0]);
  sim.scriptevent("jb:wormhole", JSON.stringify({ op: "aim", player: "Mira", place: "Fortress", x: -20, y: 80, z: 7, dim: "nether" }));
  step(mira, { x: 2.2, y: 66, z: 0.9 });
  assert.equal(mira.dimension, nether);
  assert.deepEqual(mira.location, { x: -19.5, y: 80, z: 7.5 });
});

test("a blocked landing is refused once, and tried again on stepping back in", () => {
  openGate();
  set(sim.overworld, { x: 100, y: 71, z: -50 }, "minecraft:stone"); // built over
  const p = crew("Alex", "-2", { x: 9, y: 64, z: 9 });
  step(p, { x: 1.5, y: 65, z: 0.5 });
  assert.deepEqual(p.location, { x: 1.5, y: 65, z: 0.5 });
  assert.deepEqual(p.messages, [`§7${L.blocked("Tree Farm")}`]);
  assert.equal(lines("wormhole trip refused").length, 1);
  sim.tick();
  sim.tick();
  assert.equal(p.messages.length, 1); // standing there isn't asking again
  step(p, { x: 1.5, y: 64, z: 3.5 }); // out
  set(sim.overworld, { x: 100, y: 71, z: -50 }, AIR);
  step(p, { x: 1.5, y: 65, z: 0.5 }); // back in
  assert.deepEqual(p.location, { x: 100.5, y: 70, z: -49.5 });
});

test("breaking a ring block destroys the gate, and the seed is lost", () => {
  const { cells, mira } = openGate();
  const steve = crew("Steve", "-3");
  set(sim.overworld, cells.ring[6], AIR);
  sim.breakBlock({
    player: steve,
    block: sim.overworld.getBlock(cells.ring[6]),
    brokenBlockPermutation: { type: { id: DIAMOND } },
    dimension: sim.overworld,
  });
  assert.deepEqual(gates(), []);
  assert.equal(sim.props.get("jb:gate:1"), undefined);
  assert.deepEqual(surface(cells), Array(6).fill(AIR));
  assert.equal(steve.messages.at(-1), `§7${L.collapsed}`);
  assert.equal(mira.messages.at(-1), `§7${L.collapsed}`);
  assert.equal(last(), "[jbrain] wormhole collapsed: gate 1 (Mira's) at 0 64 0 (overworld), broken by Steve");
  assert.equal(sim.overworld.spawned.length, 0); // no seed comes back
  assert.ok(!mira.inventory.slots.some((s) => s?.typeId === "jbrain:wormhole_seed"));
  // Rebuilt, the ring is just diamonds again until a new seed goes in.
  set(sim.overworld, cells.ring[6], DIAMOND);
  sim.tick();
  assert.deepEqual(surface(cells), Array(6).fill(AIR));
});

test("a ring broken without a player (a piston, a blast) is caught within a second", () => {
  const { cells } = openGate();
  set(sim.overworld, cells.ring[2], "minecraft:piston");
  for (let i = 0; i < 4; i++) sim.tick();
  assert.deepEqual(gates(), []);
  assert.deepEqual(surface(cells), Array(6).fill(AIR));
  assert.match(last(), /^\[jbrain\] wormhole collapsed: gate 1 \(Mira's\) at 0 64 0 \(overworld\)$/);
});

test("an optional corner broken, or a ring out of loaded ground, leaves the gate standing", () => {
  const cells = build(GATE, { corners: true });
  const mira = crew("Mira", "-1");
  holding(mira, "jbrain:wormhole_seed");
  plant(mira, cells.ring[0]);
  set(sim.overworld, cells.corners[0], AIR);
  sim.breakBlock({
    player: mira,
    block: sim.overworld.getBlock(cells.corners[0]),
    brokenBlockPermutation: { type: { id: DIAMOND } },
    dimension: sim.overworld,
  });
  sim.overworld.unloadedX = 0; // the whole gate's chunk unloads
  for (let i = 0; i < 4; i++) sim.tick();
  assert.deepEqual(gates(), [1]);
});

test("a removed place takes its gates dormant; opening again is then a re-aim", () => {
  const { cells, mira } = openGate();
  sim.scriptevent("jb:wormhole", JSON.stringify({ op: "forget", player: "MIRA", place: "tree farm" }));
  assert.equal(last(), "[jbrain] wormhole forgot tree farm for MIRA: 1 gate(s) went dormant");
  assert.equal(record(1).target, null);
  assert.deepEqual(surface(cells), Array(6).fill(AIR));
  step(mira, { x: 1.5, y: 65, z: 0.5 }); // a dark gate takes nobody anywhere
  assert.deepEqual(mira.location, { x: 1.5, y: 65, z: 0.5 });
  aim("Mira", "Tree Farm", { x: 100, y: 70, z: -50 });
  assert.match(last(), /costs a Power Pack/);
  // Someone else's place of the same name touches nothing.
  mira.inventory.setItem(1, new ItemStack("jbrain:power_pack", 1));
  aim("Mira", "Tree Farm", { x: 100, y: 70, z: -50 });
  sim.scriptevent("jb:wormhole", JSON.stringify({ op: "forget", player: "Steve", place: "Tree Farm" }));
  assert.equal(record(1).target.place, "Tree Farm");
});

test("a malformed request is refused and says why", () => {
  sim.scriptevent("jb:wormhole", "{not json");
  assert.match(last(), /^\[jbrain\] wormhole refused: /);
  sim.scriptevent("jb:wormhole", JSON.stringify({ op: "aim", player: "Mira", place: "X", x: 1, y: 2, z: 3 }));
  assert.equal(last(), "[jbrain] wormhole refused: Error: x, y, z and dim are required");
});
