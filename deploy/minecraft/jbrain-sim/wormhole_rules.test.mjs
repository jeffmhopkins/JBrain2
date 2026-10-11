// node --test: the wormhole gates' pure rules (wormhole_rules.js), and the Wormhole Seed's
// unlock in the item guide (rules.js). The supervisor's test_minecraft_wormhole.py runs
// this file, so CI does too.
import assert from "node:assert/strict";
import { test } from "node:test";
import * as r from "../jbrain-pack/scripts/rules.js";
import * as w from "../jbrain-pack/scripts/wormhole_rules.js";

const D = w.FRAME_BLOCK;

// A little world of blocks: everything is air unless set.
const blocks = (cells, type = D) => {
  const m = new Map(cells.map((c) => [`${c.x},${c.y},${c.z}`, type]));
  return { m, read: (x, y, z) => m.get(`${x},${y},${z}`) ?? w.AIR };
};

test("a gate's cells: ten ring blocks, four optional corners, a 2x3 inside", () => {
  const { ring, corners, interior } = w.frameCells({ axis: "x", x: 10, y: 64, z: -3 });
  assert.equal(ring.length, 10);
  assert.equal(corners.length, 4);
  assert.deepEqual(interior.map((c) => [c.x, c.y]), [[11, 65], [12, 65], [11, 66], [12, 66], [11, 67], [12, 67]]);
  assert.ok([...ring, ...corners, ...interior].every((c) => c.z === -3));
  const along = w.frameCells({ axis: "z", x: 5, y: 0, z: 0 });
  assert.ok(along.interior.every((c) => c.x === 5 && c.z >= 1 && c.z <= 2));
});

for (const axis of ["x", "z"]) {
  for (const withCorners of [false, true]) {
    test(`a ring along ${axis}, corners ${withCorners ? "in" : "out"}, is found from any of its ten blocks`, () => {
      const gate = { axis, x: 3, y: 70, z: -8 };
      const cells = w.frameCells(gate);
      const { read } = blocks(withCorners ? [...cells.ring, ...cells.corners] : cells.ring);
      for (const c of cells.ring) assert.deepEqual(w.findFrame(read, c), gate, JSON.stringify(c));
      // A corner is optional, so it never plants a gate.
      for (const c of cells.corners) assert.equal(w.findFrame(read, c), null);
    });
  }
}

test("not a gate: a block missing, a wrong block, a filled inside, the wrong size", () => {
  const gate = { axis: "x", x: 0, y: 64, z: 0 };
  const { ring, interior } = w.frameCells(gate);
  const missing = blocks(ring.slice(1));
  assert.equal(w.findFrame(missing.read, ring[2]), null);
  const gold = blocks(ring);
  gold.m.set(`${ring[0].x},${ring[0].y},${ring[0].z}`, "minecraft:gold_block");
  assert.equal(w.findFrame(gold.read, ring[3]), null);
  const filled = blocks(ring);
  filled.m.set(`${interior[4].x},${interior[4].y},${interior[4].z}`, "minecraft:dirt");
  assert.equal(w.findFrame(filled.read, ring[3]), null);
  // A 3-wide ring (the Nether portal's 4x5 is the only size) is no gate.
  const narrow = blocks(
    [[0, 65], [0, 66], [0, 67], [2, 65], [2, 66], [2, 67], [1, 64], [1, 68]].map(([x, y]) => ({ x, y, z: 0 })),
  );
  assert.equal(w.findFrame(narrow.read, { x: 0, y: 66, z: 0 }), null);
});

test("reach is measured from the gate's slab, and only the planter's own gate is theirs", () => {
  const gate = { id: 1, axis: "x", x: 0, y: 64, z: 0, dim: "overworld", planter: "Mira" };
  assert.equal(w.distanceTo(gate, { x: 2, y: 65, z: 0.5 }), 0);
  assert.equal(w.distanceTo(gate, { x: 2, y: 64, z: 5 }), 4);
  assert.ok(w.distanceTo(gate, { x: 2, y: 64, z: 5.2 }) > w.REACH);
  assert.equal(w.gateFor([gate], "mira", "overworld", { x: 1, y: 64, z: 4 }).gate, gate);
  assert.deepEqual(w.gateFor([gate], "Steve", "overworld", { x: 1, y: 64, z: 4 }), { gate: null, theirs: true });
  assert.deepEqual(w.gateFor([gate], "Mira", "nether", { x: 1, y: 64, z: 4 }), { gate: null, theirs: false });
  assert.deepEqual(w.gateFor([gate], "Mira", "overworld", { x: 1, y: 64, z: 9 }), { gate: null, theirs: false });
  // Two of theirs in reach: the nearer.
  const near = { ...gate, id: 2, z: 6 };
  assert.equal(w.gateFor([gate, near], "Mira", "overworld", { x: 1, y: 64, z: 5 }).gate, near);
});

test("aiming: the first opening is free, a re-aim costs a pack, the same place costs nothing", () => {
  const to = { place: "Tree Farm", x: 1, y: 70, z: 2, dim: "overworld" };
  const fresh = { target: null, opened: false };
  assert.equal(w.aimPlan(fresh, to, 0), "free");
  const open = { target: to, opened: true };
  assert.equal(w.aimPlan(open, { ...to, place: "tree farm" }, 0), "same");
  assert.equal(w.aimPlan(open, { ...to, place: "Mine" }, 3), "charge");
  assert.equal(w.aimPlan(open, { ...to, place: "Mine" }, 0), "no_pack");
  // Moved since: a different spot under the same name is a change.
  assert.equal(w.aimPlan(open, { ...to, x: 9 }, 1), "charge");
  // Gone dormant (its place removed): opening it again is a re-aim.
  assert.equal(w.aimPlan({ target: null, opened: true }, to, 0), "no_pack");
});

test("Dave's scriptevent is parsed strictly", () => {
  const aim = w.parseWormhole(JSON.stringify({ op: "aim", player: "Mira", place: "Base", x: 1, y: 64, z: -2, dim: "nether" }));
  assert.deepEqual(aim, { op: "aim", player: "Mira", target: { place: "Base", x: 1, y: 64, z: -2, dim: "nether" } });
  assert.deepEqual(w.parseWormhole(JSON.stringify({ op: "forget", player: "Mira", place: "Base" })), {
    op: "forget",
    player: "Mira",
    place: "Base",
  });
  for (const bad of [
    "nope",
    "[]",
    JSON.stringify({ op: "aim", place: "Base", x: 1, y: 2, z: 3, dim: "overworld" }),
    JSON.stringify({ op: "aim", player: "M", x: 1, y: 2, z: 3, dim: "overworld" }),
    JSON.stringify({ op: "aim", player: "M", place: "B", x: 1.5, y: 2, z: 3, dim: "overworld" }),
    JSON.stringify({ op: "aim", player: "M", place: "B", x: 1, z: 3, dim: "overworld" }),
    JSON.stringify({ op: "aim", player: "M", place: "B", x: 1, y: 2, z: 3, dim: "aether" }),
    JSON.stringify({ op: "warp", player: "M", place: "B" }),
  ]) {
    assert.throws(() => w.parseWormhole(bad), bad);
  }
});

test("a gate record is read whole or not at all", () => {
  const g = { id: 3, axis: "z", x: 1, y: 2, z: 3, dim: "the_end", planter: "Mira" };
  assert.deepEqual(w.parseGate(JSON.stringify(g)), { target: null, opened: false, ...g });
  for (const bad of [undefined, "{", JSON.stringify({ ...g, axis: "y" }), JSON.stringify({ ...g, dim: "moon" })]) {
    assert.equal(w.parseGate(bad), null);
  }
  assert.deepEqual(w.landing({ x: -4, y: 70, z: 9 }), { x: -3.5, y: 70, z: 9.5 });
  assert.deepEqual(w.blockOf({ x: -0.5, y: 64.9, z: 2.1 }), { x: -1, y: 64, z: 2 });
});

test("the Wormhole Seed unlocks on popped chorus fruit, only once a Power Pack has been made", () => {
  const fruit = "minecraft:popped_chorus_fruit";
  assert.deepEqual(r.unlocksFrom([], [fruit]), ["tricorder"]);
  assert.deepEqual(r.unlocksFrom(["power_pack"], [fruit]), ["power_pack", "tricorder"]);
  assert.deepEqual(r.unlocksFrom(["power_pack", "power_pack_uses"], [fruit]), [
    "power_pack",
    "power_pack_uses",
    "tricorder",
    "wormhole_seed",
  ]);
  // A pack and the fruit held together count at once.
  assert.deepEqual(r.unlocksFrom([], [fruit, r.POWER_PACK]), ["power_pack_uses", "tricorder", "wormhole_seed"]);
});
