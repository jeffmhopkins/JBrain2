// node --test: the Replicator (plan §M15) — its pure rules, and replicator.js against the
// fake server with blocks (replicator_fake.mjs as node_modules/@minecraft/server). The
// supervisor's test_minecraft_replicator.py lays it out and runs this file, so CI does too.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { ItemStack, blocks, sim } from "@minecraft/server";
import * as r from "./pack/scripts/replicator_rules.js";
import { initReplicator } from "./pack/scripts/replicator.js";

initReplicator();
sim.startup();

const IRON = "minecraft:iron_ingot";
const AT = { x: 0, y: 64, z: 0 };
const program = (player, target) => {
  sim.logs.length = 0;
  sim.scriptevent("jb:replicator", JSON.stringify({ player, target }));
  return sim.logs.at(-1);
};
const count = (p, id) => p.inventory.slots.reduce((n, s) => n + (s?.typeId === id ? s.amount : 0), 0);
const crafter = (name = "Mira", id = "-7") => {
  const p = sim.player(name, id);
  p.inventory.setItem(0, new ItemStack(r.REPLICATOR, 1));
  p.selectedSlotIndex = 0;
  return p;
};
const give = (p, id, n, from = 1) => {
  for (let i = from; n > 0; i++) {
    p.inventory.setItem(i, new ItemStack(id, Math.min(64, n)));
    n -= 64;
  }
};
// A programmed replicator at AT with a chest on top, one beside it and one below.
const rig = (target = IRON) => {
  const p = crafter();
  give(p, target, 512);
  program(p.name, target);
  blocks.place(p, AT);
  return {
    p,
    input: blocks.container({ x: 0, y: 65, z: 0 }),
    fuel: blocks.container({ x: 1, y: 64, z: 0 }),
    output: blocks.container({ x: 0, y: 63, z: 0 }, "minecraft:hopper", 5),
  };
};

beforeEach(() => {
  sim.reset();
  blocks.reset();
});

// --- the pure rules ------------------------------------------------------------------------

test("the target's name: an ingot is its metal, as canon's Iron Replicator", () => {
  assert.equal(r.programmedName(IRON), "Iron Replicator");
  assert.equal(r.programmedName("minecraft:diamond"), "Diamond Replicator");
  assert.equal(r.programmedName("minecraft:oak_log"), "Oak Log Replicator");
  assert.equal(r.programmedLore(IRON), "Burns any block into iron");
});

test("a pattern is 8 full stacks of a 64-stacker, held over a Replicator", () => {
  const held = { typeId: r.REPLICATOR };
  assert.equal(r.PATTERN, 512);
  assert.equal(r.programCheck({ held, target: IRON, maxAmount: 64, have: 512 }), null);
  assert.deepEqual(r.programCheck({ held, target: IRON, maxAmount: 64, have: 511 }), { code: "too_few", detail: 511 });
  assert.deepEqual(r.programCheck({ held, target: "minecraft:ender_pearl", maxAmount: 16, have: 999 }), { code: "stack", detail: 16 });
  assert.deepEqual(r.programCheck({ held, target: "minecraft:diamond_pickaxe", maxAmount: 1, have: 999 }), { code: "stack", detail: 1 });
  assert.deepEqual(r.programCheck({ held, target: "minecraft:nope", maxAmount: undefined, have: 0 }), { code: "unknown_item" });
  assert.deepEqual(r.programCheck({ held: { typeId: "minecraft:dirt" }, target: IRON, maxAmount: 64, have: 512 }), { code: "not_holding" });
  assert.deepEqual(r.programCheck({ held: undefined, target: IRON, maxAmount: 64, have: 512 }), { code: "not_holding" });
  const same = { typeId: r.PROGRAMMED, target: IRON };
  assert.deepEqual(r.programCheck({ held: same, target: IRON, maxAmount: 64, have: 512 }), { code: "same" });
  assert.equal(r.programCheck({ held: same, target: "minecraft:diamond", maxAmount: 64, have: 512 }), null);
});

test("the pattern comes out slot by slot, exactly", () => {
  const slots = [
    { i: 2, typeId: IRON, amount: 64 }, { i: 3, typeId: "minecraft:dirt", amount: 64 },
    { i: 5, typeId: IRON, amount: 40 }, { i: 9, typeId: IRON, amount: 64 },
  ];
  assert.deepEqual(r.takePlan(slots, IRON, 100), [{ i: 2, left: 0 }, { i: 5, left: 4 }]);
  assert.equal(r.takePlan(slots, IRON, 169), null);
});

test("fuel burns for vanilla's times; nether wood and plain blocks don't", () => {
  assert.equal(r.fuelTicks("minecraft:coal"), 1600);
  assert.equal(r.fuelTicks("minecraft:coal_block"), 16000);
  assert.equal(r.fuelTicks("minecraft:oak_planks"), 300);
  assert.equal(r.fuelTicks("minecraft:spruce_log"), 300);
  assert.equal(r.fuelTicks("minecraft:oak_slab"), 150);
  assert.equal(r.fuelTicks("minecraft:crimson_planks"), 0);
  assert.equal(r.fuelTicks("minecraft:cobblestone"), 0);
  assert.equal(r.fuelTicks("minecraft:stone_slab"), 0);
});

test("one turn: fuel lit only when needed, and idle for want of input, fuel or room", () => {
  const rec = { target: IRON, fuel: 0 };
  assert.equal(r.turn(rec, { input: true, burn: 1600, room: false }).state, "no_output");
  assert.equal(r.turn(rec, { input: false, burn: 1600, room: true }).state, "no_input");
  assert.equal(r.turn(rec, { input: true, burn: 0, room: true }).state, "no_fuel");
  assert.deepEqual(r.turn(rec, { input: true, burn: 1600, room: true }),
    { state: "running", takeInput: true, takeFuel: true, make: true, fuel: 1400 });
  assert.equal(r.turn({ target: IRON, fuel: 200 }, { input: true, burn: 0, room: true }).takeFuel, false);
});

test("a record survives only as a target; anything else is no record", () => {
  assert.equal(r.recordKey("minecraft:overworld", { x: 1, y: -2, z: 3 }), "jb:replicator:overworld:1,-2,3");
  assert.equal(r.parseRecord(undefined), undefined);
  assert.equal(r.parseRecord("nope"), undefined);
  assert.deepEqual(r.parseRecord('{"target":"minecraft:diamond","fuel":-4}'),
    { target: "minecraft:diamond", fuel: 0, state: "no_input" });
});

test("only a vanilla item id can be asked for", () => {
  assert.deepEqual(r.parseProgram('{"player":"Mira","target":"minecraft:diamond"}'), { player: "Mira", target: "minecraft:diamond" });
  for (const bad of ['{"player":"Mira","target":"jbrain:power_pack"}', '{"player":"","target":"minecraft:dirt"}', "[]", '{"player":"Mira","target":"minecraft:dirt; kill @a"}']) {
    assert.throws(() => r.parseProgram(bad), bad);
  }
});

// --- programming ---------------------------------------------------------------------------

test("programming takes exactly 512 and names the held Replicator after its target", () => {
  const p = crafter();
  give(p, IRON, 600); // 9 slots of iron, the last short
  p.inventory.setItem(20, new ItemStack("minecraft:dirt", 64));
  assert.match(program("mira", IRON), /^\[jbrain\] replicator programmed: Mira minecraft:iron_ingot as Iron Replicator$/);
  assert.equal(count(p, IRON), 88);
  assert.equal(count(p, "minecraft:dirt"), 64);
  const held = p.inventory.slots[0];
  assert.equal(held.typeId, r.PROGRAMMED);
  assert.equal(held.nameTag, "Iron Replicator");
  assert.deepEqual(held.lore, ["Burns any block into iron"]);
  assert.equal(held.getDynamicProperty(r.TARGET), IRON);
});

test("too few, a 16-stacker, an unstackable, no such item, or nothing held: nothing changes", () => {
  const p = crafter();
  give(p, IRON, 511);
  assert.match(program("Mira", IRON), /refused: too_few 511$/);
  assert.equal(count(p, IRON), 511);
  assert.match(program("Mira", "minecraft:ender_pearl"), /refused: stack 16$/);
  assert.match(program("Mira", "minecraft:diamond_pickaxe"), /refused: stack 1$/);
  assert.match(program("Mira", "minecraft:not_a_thing"), /refused: unknown_item$/);
  p.selectedSlotIndex = 5;
  assert.match(program("Mira", IRON), /refused: not_holding$/);
  assert.equal(p.inventory.slots[0].typeId, r.REPLICATOR);
  assert.match(program("Nobody", IRON), /refused: offline Nobody$/);
  assert.match(program("Mira", "jbrain:power_pack"), /refused: bad_request/);
});

test("reprogramming takes 8 new stacks and the old pattern is gone; the same one is refused", () => {
  const p = crafter();
  give(p, IRON, 512);
  program("Mira", IRON);
  give(p, IRON, 512);
  assert.match(program("Mira", IRON), /refused: same$/);
  assert.equal(count(p, IRON), 512);
  give(p, "minecraft:diamond", 512, 10);
  assert.match(program("Mira", "minecraft:diamond"), /programmed: .* as Diamond Replicator$/);
  assert.equal(count(p, "minecraft:diamond"), 0);
  assert.equal(count(p, IRON), 512); // the iron already taken isn't given back, nor is this
  const held = p.inventory.slots[0];
  assert.equal(held.nameTag, "Diamond Replicator");
  assert.equal(held.getDynamicProperty(r.TARGET), "minecraft:diamond");
  assert.equal(count(p, "jbrain:power_pack"), 0); // packs aren't paid twice
});

// --- placing and breaking ------------------------------------------------------------------

test("placed, it remembers its target; broken, it gives back the same named item", () => {
  const p = crafter();
  give(p, IRON, 512);
  program("Mira", IRON);
  const block = blocks.place(p, AT);
  assert.equal(block.permutation.getState(r.STATE), true);
  assert.equal(p.inventory.slots[0], undefined);
  assert.match(sim.props.get(r.recordKey("minecraft:overworld", AT)), /iron_ingot/);
  blocks.breakBy(p, AT);
  assert.equal(sim.props.has(r.recordKey("minecraft:overworld", AT)), false);
  const [{ item }] = sim.overworld.spawned;
  assert.equal(item.typeId, r.PROGRAMMED);
  assert.equal(item.nameTag, "Iron Replicator");
  assert.equal(item.getDynamicProperty(r.TARGET), IRON);
  // And it goes down again just as it was.
  p.inventory.setItem(0, item);
  blocks.place(p, { x: 5, y: 64, z: 5 });
  assert.match(sim.props.get(r.recordKey("minecraft:overworld", { x: 5, y: 64, z: 5 })), /iron_ingot/);
});

test("a blank one places blank, makes nothing, and breaks back to a blank", () => {
  const p = crafter();
  const block = blocks.place(p, AT);
  assert.equal(block.permutation.getState(r.STATE), false);
  assert.equal(sim.props.has(r.recordKey("minecraft:overworld", AT)), false);
  blocks.breakBy(p, AT);
  const [{ item }] = sim.overworld.spawned;
  assert.equal(item.typeId, r.REPLICATOR);
  assert.equal(item.nameTag, undefined);
});

test("using a placed one tells the player what it makes and why it waits", () => {
  const { p } = rig();
  blocks.interact(p, AT);
  assert.match(p.messages.at(-1), /Iron Replicator§r · waiting for a block on top/);
  assert.match(p.messages.at(-1), /fuel beside it/);
});

// --- running -------------------------------------------------------------------------------

test("one of the target per turn from any block, fuel beside, out below", () => {
  const { input, fuel, output } = rig();
  input.setItem(4, new ItemStack("minecraft:coal", 3)); // fuel on top isn't burned as a block
  input.setItem(5, new ItemStack("minecraft:cobblestone", 10));
  fuel.setItem(0, new ItemStack("minecraft:coal", 1));
  blocks.ticks(3);
  assert.equal(output.getItem(0).typeId, IRON);
  assert.equal(output.getItem(0).amount, 3);
  assert.equal(input.getItem(5).amount, 7);
  assert.equal(input.getItem(4).amount, 3);
  assert.equal(fuel.getItem(0), undefined); // the coal is lit once
});

test("coal lasts 8; lava leaves its bucket; no fuel is idle", () => {
  const { input, fuel, output } = rig();
  input.setItem(0, new ItemStack("minecraft:cobblestone", 64));
  fuel.setItem(0, new ItemStack("minecraft:coal", 1));
  blocks.ticks(10);
  const made = () => output.slots.reduce((n, s) => n + (s?.amount ?? 0), 0);
  assert.equal(made(), 8);
  assert.match(sim.props.get(r.recordKey("minecraft:overworld", AT)), /no_fuel/);
  fuel.setItem(3, new ItemStack("minecraft:lava_bucket", 1));
  blocks.ticks(1);
  assert.equal(made(), 9);
  assert.equal(fuel.getItem(3).typeId, "minecraft:bucket");
});

test("a full output waits, taking nothing", () => {
  const { input, fuel, output } = rig();
  for (let i = 0; i < 5; i++) output.setItem(i, new ItemStack("minecraft:dirt", 64));
  input.setItem(0, new ItemStack("minecraft:cobblestone", 4));
  fuel.setItem(0, new ItemStack("minecraft:coal", 2));
  blocks.ticks(3);
  assert.equal(input.getItem(0).amount, 4);
  assert.equal(fuel.getItem(0).amount, 2);
  output.setItem(2, new ItemStack(IRON, 63));
  blocks.ticks(1);
  assert.equal(output.getItem(2).amount, 64);
});

test("away from everyone it makes nothing, and doesn't catch up when its land wakes", () => {
  const { input, fuel, output } = rig();
  input.setItem(0, new ItemStack("minecraft:cobblestone", 64));
  fuel.setItem(0, new ItemStack("minecraft:coal_block", 1));
  blocks.unload(AT);
  blocks.ticks(50);
  assert.equal(output.getItem(0), undefined);
  blocks.load(AT);
  blocks.ticks(1);
  assert.equal(output.getItem(0).amount, 1);
});

test("nothing to put it in, or only items on top: it waits", () => {
  const p = crafter();
  give(p, IRON, 512);
  program("Mira", IRON);
  blocks.place(p, AT);
  const input = blocks.container({ x: 0, y: 65, z: 0 });
  const fuel = blocks.container({ x: 0, y: 64, z: -1 });
  input.setItem(0, new ItemStack("minecraft:stick", 64)); // an item, not a block
  fuel.setItem(0, new ItemStack("minecraft:coal", 1));
  blocks.ticks(2);
  assert.equal(input.getItem(0).amount, 64);
  assert.match(sim.props.get(r.recordKey("minecraft:overworld", AT)), /no_output/);
  const output = blocks.container({ x: 0, y: 63, z: 0 });
  blocks.ticks(1);
  assert.match(sim.props.get(r.recordKey("minecraft:overworld", AT)), /no_input/);
  assert.equal(output.getItem(0), undefined);
});

// --- §M19: the guide entry -----------------------------------------------------------------

test("8 Power Packs on a player unlock the Replicator's guide entry, once", () => {
  assert.equal(r.unlocksReplicator([{ typeId: r.POWER_PACK, amount: 7 }]), false);
  assert.equal(r.unlocksReplicator([{ typeId: r.POWER_PACK, amount: 5 }, { typeId: r.POWER_PACK, amount: 3 }]), true);
  const p = sim.player("Pack Rat", "-9");
  sim.props.set("jb:known:-9", JSON.stringify(["power_pack", "tricorder"]));
  p.inventory.setItem(0, new ItemStack(r.POWER_PACK, 7));
  sim.tick();
  assert.equal(sim.props.get("jb:known:-9"), JSON.stringify(["power_pack", "tricorder"]));
  p.inventory.setItem(1, new ItemStack(r.POWER_PACK, 1));
  sim.tick();
  assert.equal(sim.props.get("jb:known:-9"), JSON.stringify(["power_pack", "replicator", "tricorder"]));
  p.inventory.slots.fill(undefined);
  sim.tick();
  assert.match(sim.props.get("jb:known:-9"), /replicator/); // never taken back
});
